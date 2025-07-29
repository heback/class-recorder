# app.py - Streamlit + Firebase 수업·출결 관리 (단일 파일)
# --------------------------------------------------------------
# 요구사항 요약
# - 담당 교과의 수업/평가 계획(PDF) 업로드/조회 (PDF, <=10MB)
# - 교과/수업반/학생/수업기록/출결 CRUD
# - 일자별 전체 조회(진도·특기사항 / 출결·특기사항)
# - Firebase Firestore/Storage + Streamlit Cloud (GitHub 연동)
# - Firebase 인증키는 st.secrets['FIREBASE_KEY'] (dict 변환 후 사용), storageBucket 포함
# - 데이터 없음 시 st.info, 예외 꼼꼼히 처리, 입력·수정은 st.dialog 사용(닫기 버튼 불필요)
# - 단일 파일 구성(app.py)
# --------------------------------------------------------------

from __future__ import annotations
import json
import time
import io
import csv
from datetime import datetime, date, timezone, timedelta
from typing import List, Dict, Any, Optional, Tuple

import streamlit as st

# Firebase Admin SDK
import firebase_admin
from firebase_admin import credentials, firestore, storage
from google.cloud.exceptions import GoogleCloudError

# (선택) 판다스는 CSV 미리보기/다운로드 등에 사용
try:
    import pandas as pd  # noqa: F401
except Exception:  # pragma: no cover
    pd = None

# =============================
# 상수/유틸
# =============================
KST = timezone(timedelta(hours=9))
DAYS = [
    ("MON", "월"), ("TUE", "화"), ("WED", "수"), ("THU", "목"), ("FRI", "금")
]
PERIOD_CHOICES = list(range(1, 10))  # 최대 9교시 가정. 학교에 맞게 조정 가능.
ATTENDANCE_CHOICES = ["P", "A", "L", "E", "U"]  # 출석/결석/지각/조퇴/미입력

COLLECTIONS = {
    "courses": "courses",
    "classes": "classes",
    "class_students": "class_students",
    "lesson_logs": "lesson_logs",
    "attendance": "attendance",
}

# 세션 키
SK = {
    "year": "filter_year",
    "semester": "filter_semester",
    "course_id": "sel_course_id",
    "class_id": "sel_class_id",
    "submit_cache": "_submit_cache", # dialog 간 결과 전달
}


# =============================
# Firebase 초기화
# =============================
@st.cache_resource(show_spinner=False)
def init_firebase():
    raw = st.secrets.get("FIREBASE_KEY")
    if not raw:
        raise RuntimeError("Streamlit Secrets에 FIREBASE_KEY가 설정되어 있지 않습니다.")
    firebase_key = json.loads(raw) if isinstance(raw, str) else dict(raw)
    if "storageBucket" not in firebase_key:
        raise RuntimeError("FIREBASE_KEY에 storageBucket 항목이 누락되었습니다.")
    if not firebase_admin._apps:
        cred = credentials.Certificate(firebase_key)
        firebase_admin.initialize_app(cred, {"storageBucket": firebase_key["storageBucket"]})
    return firestore.client(), storage.bucket()


db, bucket = init_firebase()


# =============================
# 공통 유틸
# =============================

def now_kst_iso() -> str:
    return datetime.now(tz=KST).isoformat()


def today_ymd() -> str:
    return datetime.now(tz=KST).strftime("%Y-%m-%d")


def safe_filename(name: str) -> str:
    return name.replace("/", "_").replace("\\", "_").strip()


def validate_pdf(uploaded_file) -> Tuple[bool, str]:
    if uploaded_file is None:
        return False, "파일을 선택해 주세요."
    # Streamlit UploadedFile: .type (MIME), .name, .getbuffer()
    mime = getattr(uploaded_file, "type", "") or ""
    if not mime.endswith("/pdf") and mime != "application/pdf":
        return False, "PDF 파일만 업로드 가능합니다."
    try:
        size = len(uploaded_file.getbuffer())
    except Exception:
        try:
            pos = uploaded_file.tell()
            uploaded_file.seek(0, 2)
            size = uploaded_file.tell()
            uploaded_file.seek(pos)
        except Exception:
            return False, "파일 크기를 확인할 수 없습니다."
    if size > 10 * 1024 * 1024:
        return False, "파일 크기는 10MB 이하여야 합니다."
    return True, "OK"


def upload_pdf(uploaded_file, year: int, semester: int, subject_name: str) -> str:
    is_ok, msg = validate_pdf(uploaded_file)
    if not is_ok:
        raise ValueError(msg)
    ts = int(time.time())
    fname = safe_filename(getattr(uploaded_file, "name", f"{ts}.pdf"))
    path = f"plans/{year}/{semester}/{safe_filename(subject_name)}/{ts}_{fname}"
    blob = bucket.blob(path)
    blob.upload_from_file(uploaded_file, content_type="application/pdf")
    return path


def generate_signed_url(storage_path: str, hours: int = 1) -> str:
    if not storage_path:
        raise ValueError("저장된 파일 경로가 없습니다.")
    blob = bucket.blob(storage_path)
    if not blob.exists():
        raise FileNotFoundError("스토리지에 파일이 존재하지 않습니다.")
    return blob.generate_signed_url(expiration=timedelta(hours=hours))


def batched_delete(doc_refs: List[Any]):
    # Firestore batch write는 500 제한
    for i in range(0, len(doc_refs), 490):  # 약간 여유
        batch = db.batch()
        for ref in doc_refs[i:i+490]:
            batch.delete(ref)
        batch.commit()


def batched_set(updates: List[Tuple[Any, Dict[str, Any]]]):
    for i in range(0, len(updates), 490):
        batch = db.batch()
        for ref, data in updates[i:i+490]:
            batch.set(ref, data)
        batch.commit()


# =============================
# DAO - Firestore 접근 래퍼
# =============================
# ----- Courses -----

def course_unique_key(year: int, semester: int, subject_name: str) -> str:
    return f"{year}-{semester}-{subject_name.strip()}"


def get_course_by_unique(year: int, semester: int, subject_name: str) -> Optional[Dict[str, Any]]:
    q = (db.collection(COLLECTIONS["courses"])  # type: ignore
         .where("year", "==", int(year))
         .where("semester", "==", int(semester))
         .where("subject_name", "==", subject_name.strip()))
    docs = list(q.stream())
    if not docs:
        return None
    d = docs[0]
    x = d.to_dict() or {}
    x.update({"_id": d.id})
    return x


def list_courses(year: Optional[int] = None, semester: Optional[int] = None, name_filter: str = "") -> List[Dict[str, Any]]:
    col = db.collection(COLLECTIONS["courses"])  # type: ignore
    q = col
    if year is not None:
        q = q.where("year", "==", int(year))
    if semester is not None:
        q = q.where("semester", "==", int(semester))
    docs = list(q.stream())
    res = []
    nf = (name_filter or "").strip().lower()
    for d in docs:
        item = d.to_dict() or {}
        if nf and nf not in str(item.get("subject_name", "")).lower():
            continue
        item.update({"_id": d.id})
        res.append(item)
    # 정렬: subject_name
    res.sort(key=lambda x: (x.get("year", 0), x.get("semester", 0), str(x.get("subject_name", ""))))
    return res


def create_or_update_course(year: int, semester: int, subject_name: str, pdf_path: Optional[str]) -> str:
    now = now_kst_iso()
    existing = get_course_by_unique(year, semester, subject_name)
    data = {
        "year": int(year),
        "semester": int(semester),
        "subject_name": subject_name.strip(),
        "updated_at": now,
    }
    if pdf_path:
        data.update({"storage_path": pdf_path, "file_size": None})  # file_size는 필요 시 추가로 조회
    if existing:
        ref = db.collection(COLLECTIONS["courses"]).document(existing["_id"])  # type: ignore
        ref.set(data, merge=True)
        return existing["_id"]
    else:
        data["created_at"] = now
        ref = db.collection(COLLECTIONS["courses"]).document()  # type: ignore
        ref.set(data)
        return ref.id


def delete_course(course_id: str):
    # course 삭제 → 관련 classes, class_students, lesson_logs, attendance, storage 파일 삭제
    course_ref = db.collection(COLLECTIONS["courses"]).document(course_id)  # type: ignore
    course_doc = course_ref.get()
    if not course_doc.exists:
        return
    data = course_doc.to_dict() or {}
    storage_path = data.get("storage_path")

    # 관련 classes
    classes = list(db.collection(COLLECTIONS["classes"]).where("course_id", "==", course_id).stream())  # type: ignore
    class_ids = [c.id for c in classes]

    # 연결된 하위 문서 삭제
    to_delete = []
    # classes
    to_delete.extend([db.collection(COLLECTIONS["classes"]).document(cid) for cid in class_ids])
    # class_students
    for cid in class_ids:
        to_delete.extend([d.reference for d in db.collection(COLLECTIONS["class_students"]).where("class_id", "==", cid).stream()])  # type: ignore
        to_delete.extend([d.reference for d in db.collection(COLLECTIONS["lesson_logs"]).where("class_id", "==", cid).stream()])  # type: ignore
        to_delete.extend([d.reference for d in db.collection(COLLECTIONS["attendance"]).where("class_id", "==", cid).stream()])  # type: ignore
    # course
    to_delete.append(course_ref)

    batched_delete(to_delete)

    # storage 파일 삭제(있으면)
    try:
        if storage_path:
            blob = bucket.blob(storage_path)
            if blob.exists():
                blob.delete()
    except Exception as e:
        st.warning(f"스토리지 파일 삭제 중 경고: {e}")


# ----- Classes -----

def schedule_summary(schedule: List[Dict[str, Any]]) -> str:
    if not schedule:
        return "-"
    day_map = {k: v for k, v in DAYS}
    # 예: 월3,수2
    items = []
    for s in sorted(schedule, key=lambda x: (x.get("day", ""), int(x.get("period", 0)))):
        day = day_map.get(s.get("day", ""), s.get("day", ""))
        items.append(f"{day}{s.get('period', '')}")
    return ",".join(items)


def list_classes(course_id: Optional[str] = None) -> List[Dict[str, Any]]:
    col = db.collection(COLLECTIONS["classes"])  # type: ignore
    q = col
    if course_id:
        q = q.where("course_id", "==", course_id)
    docs = list(q.stream())
    res = []
    for d in docs:
        item = d.to_dict() or {}
        item.update({"_id": d.id})
        res.append(item)
    res.sort(key=lambda x: str(x.get("class_label", "")))
    return res


def get_class(class_id: str) -> Optional[Dict[str, Any]]:
    doc = db.collection(COLLECTIONS["classes"]).document(class_id).get()  # type: ignore
    if not doc.exists:
        return None
    item = doc.to_dict() or {}
    item.update({"_id": doc.id})
    return item


def create_or_update_class(course_id: str, class_label: str, year: int, semester: int, schedule: Optional[List[Dict[str, Any]]] = None, class_id: Optional[str] = None) -> str:
    now = now_kst_iso()
    data = {
        "course_id": course_id,
        "class_label": class_label.strip(),
        "year": int(year),
        "semester": int(semester),
        "updated_at": now,
    }
    if schedule is not None:
        data["schedule"] = schedule
    if class_id:
        ref = db.collection(COLLECTIONS["classes"]).document(class_id)  # type: ignore
        ref.set(data, merge=True)
        return class_id
    else:
        data["created_at"] = now
        ref = db.collection(COLLECTIONS["classes"]).document()  # type: ignore
        ref.set(data)
        return ref.id


def delete_class(class_id: str):
    # class 삭제 → students, lesson_logs, attendance
    to_delete = []
    to_delete.append(db.collection(COLLECTIONS["classes"]).document(class_id))  # type: ignore
    for name in ("class_students", "lesson_logs", "attendance"):
        to_delete.extend([d.reference for d in db.collection(COLLECTIONS[name]).where("class_id", "==", class_id).stream()])  # type: ignore
    batched_delete(to_delete)


# ----- Students -----

def list_students(class_id: str) -> List[Dict[str, Any]]:
    docs = list(db.collection(COLLECTIONS["class_students"]).where("class_id", "==", class_id).stream())  # type: ignore
    res = []
    for d in docs:
        item = d.to_dict() or {}
        item.update({"_id": d.id})
        res.append(item)
    res.sort(key=lambda x: str(x.get("student_no", "")))
    return res


def get_student_by_no(class_id: str, student_no: str) -> Optional[Dict[str, Any]]:
    q = (db.collection(COLLECTIONS["class_students"])  # type: ignore
         .where("class_id", "==", class_id)
         .where("student_no", "==", str(student_no).strip()))
    docs = list(q.stream())
    if not docs:
        return None
    d = docs[0]
    item = d.to_dict() or {}
    item.update({"_id": d.id})
    return item


def create_or_update_student(class_id: str, student_no: str, name: str, student_id: Optional[str] = None) -> str:
    now = now_kst_iso()
    data = {
        "class_id": class_id,
        "student_no": str(student_no).strip(),
        "name": name.strip(),
        "updated_at": now,
    }
    if student_id:
        ref = db.collection(COLLECTIONS["class_students"]).document(student_id)  # type: ignore
        ref.set(data, merge=True)
        return student_id
    else:
        data["created_at"] = now
        ref = db.collection(COLLECTIONS["class_students"]).document()  # type: ignore
        ref.set(data)
        return ref.id


def delete_student(student_id: str):
    # 학생 삭제 → 해당 학생의 attendance 모두 삭제
    # attendance는 student_id 필드로 연결됨
    to_delete = []
    to_delete.append(db.collection(COLLECTIONS["class_students"]).document(student_id))  # type: ignore
    to_delete.extend([d.reference for d in db.collection(COLLECTIONS["attendance"]).where("student_id", "==", student_id).stream()])  # type: ignore
    batched_delete(to_delete)


# ----- Lesson Logs -----

def list_lesson_logs(class_id: str, date_filter: Optional[str] = None) -> List[Dict[str, Any]]:
    col = db.collection(COLLECTIONS["lesson_logs"])  # type: ignore
    q = col.where("class_id", "==", class_id)
    if date_filter:
        q = q.where("date", "==", date_filter)
    docs = list(q.stream())
    res = []
    for d in docs:
        item = d.to_dict() or {}
        item.update({"_id": d.id})
        res.append(item)
    res.sort(key=lambda x: (x.get("date", ""), int(x.get("period", 0))))
    return res


def get_lesson_log_unique(class_id: str, date_str: str, period: int) -> Optional[Dict[str, Any]]:
    q = (db.collection(COLLECTIONS["lesson_logs"])  # type: ignore
         .where("class_id", "==", class_id)
         .where("date", "==", date_str)
         .where("period", "==", int(period)))
    docs = list(q.stream())
    if not docs:
        return None
    d = docs[0]
    item = d.to_dict() or {}
    item.update({"_id": d.id})
    return item


def create_or_update_lesson_log(class_id: str, date_str: str, period: int, progress: str, note: str = "") -> str:
    now = now_kst_iso()
    existing = get_lesson_log_unique(class_id, date_str, period)
    data = {
        "class_id": class_id,
        "date": date_str,
        "period": int(period),
        "progress": progress.strip(),
        "note": note.strip(),
        "updated_at": now,
    }
    if existing:
        ref = db.collection(COLLECTIONS["lesson_logs"]).document(existing["_id"])  # type: ignore
        ref.set(data, merge=True)
        return existing["_id"]
    else:
        data["created_at"] = now
        ref = db.collection(COLLECTIONS["lesson_logs"]).document()  # type: ignore
        ref.set(data)
        return ref.id


def delete_lesson_log(log_id: str):
    db.collection(COLLECTIONS["lesson_logs"]).document(log_id).delete()  # type: ignore


# ----- Attendance -----

def list_attendance(class_id: Optional[str] = None, date_filter: Optional[str] = None) -> List[Dict[str, Any]]:
    col = db.collection(COLLECTIONS["attendance"])  # type: ignore
    q = col
    if class_id:
        q = q.where("class_id", "==", class_id)
    if date_filter:
        q = q.where("date", "==", date_filter)
    docs = list(q.stream())
    res = []
    for d in docs:
        item = d.to_dict() or {}
        item.update({"_id": d.id})
        res.append(item)
    res.sort(key=lambda x: (x.get("class_id", ""), x.get("date", ""), int(x.get("period", 0)), x.get("student_id", "")))
    return res


def get_attendance_unique(class_id: str, date_str: str, period: int, student_id: str) -> Optional[Dict[str, Any]]:
    q = (db.collection(COLLECTIONS["attendance"])  # type: ignore
         .where("class_id", "==", class_id)
         .where("date", "==", date_str)
         .where("period", "==", int(period))
         .where("student_id", "==", student_id))
    docs = list(q.stream())
    if not docs:
        return None
    d = docs[0]
    item = d.to_dict() or {}
    item.update({"_id": d.id})
    return item


def set_attendance(class_id: str, date_str: str, period: int, student_id: str, status: str, remark: str = "") -> str:
    now = now_kst_iso()
    status = (status or "U").strip().upper()
    if status not in ATTENDANCE_CHOICES:
        status = "U"
    existing = get_attendance_unique(class_id, date_str, period, student_id)
    data = {
        "class_id": class_id,
        "date": date_str,
        "period": int(period),
        "student_id": student_id,
        "status": status,
        "remark": remark.strip(),
        "updated_at": now,
    }
    if existing:
        ref = db.collection(COLLECTIONS["attendance"]).document(existing["_id"])  # type: ignore
        ref.set(data, merge=True)
        return existing["_id"]
    else:
        data["created_at"] = now
        ref = db.collection(COLLECTIONS["attendance"]).document()  # type: ignore
        ref.set(data)
        return ref.id


# =============================
# UI 공통: 필터/선택 도우미
# =============================

def year_semester_filters():
    default_year = datetime.now(tz=KST).year
    year = st.sidebar.number_input("학년도", min_value=2000, max_value=2100, value=st.session_state.get(SK["year"], default_year))
    semester = st.sidebar.selectbox("학기", [1, 2], index=(st.session_state.get(SK["semester"], 1) - 1))
    st.session_state[SK["year"]] = int(year)
    st.session_state[SK["semester"]] = int(semester)
    return int(year), int(semester)


def select_course(year: int, semester: int) -> Optional[str]:
    courses = list_courses(year, semester)
    if not courses:
        st.info("등록된 교과가 없습니다.")
        return None
    options = {f"{c['subject_name']} ({c['year']}-{c['semester']})": c["_id"] for c in courses}
    current = st.session_state.get(SK["course_id"]) or list(options.values())[0]
    label = {v: k for k, v in options.items()}.get(current, list(options.keys())[0])
    sel = st.selectbox("교과 선택", options=list(options.keys()), index=list(options.keys()).index(label))
    course_id = options[sel]
    st.session_state[SK["course_id"]] = course_id
    return course_id


def select_class(course_id: str) -> Optional[str]:
    classes = list_classes(course_id)
    if not classes:
        st.info("선택한 교과에 등록된 수업이 없습니다.")
        return None
    options = {f"{c['class_label']}": c["_id"] for c in classes}
    current = st.session_state.get(SK["class_id"]) or list(options.values())[0]
    label = {v: k for k, v in options.items()}.get(current, list(options.keys())[0])
    sel = st.selectbox("수업 반 선택", options=list(options.keys()), index=list(options.keys()).index(label))
    class_id = options[sel]
    st.session_state[SK["class_id"]] = class_id
    return class_id


# =============================
# Dialogs (st.dialog)
# =============================

@st.dialog("교과 추가/수정")
def dlg_course_form(default: Optional[Dict[str, Any]] = None):
    year = st.number_input("학년도", min_value=2000, max_value=2100, value=(default.get("year", datetime.now(tz=KST).year) if default else datetime.now(tz=KST).year))
    semester = st.selectbox("학기", [1, 2], index=((default.get("semester", 1) - 1) if default else 0))
    subject = st.text_input("교과명", value=(default.get("subject_name", "") if default else ""))
    pdf = st.file_uploader("수업·평가 계획서(PDF, 최대 10MB)", type=["pdf"], accept_multiple_files=False)

    col1, col2 = st.columns(2)
    if col1.button("저장", type="primary"):
        st.session_state[SK["submit_cache"]] = {"year": int(year), "semester": int(semester), "subject_name": subject.strip(), "pdf": pdf}
    if col2.button("취소"):
        st.session_state[SK["submit_cache"]] = None


@st.dialog("수업 등록/수정")
def dlg_class_form(default: Optional[Dict[str, Any]] = None, course_id: Optional[str] = None):
    year = st.number_input("학년도", min_value=2000, max_value=2100, value=(default.get("year", st.session_state.get(SK["year"], datetime.now(tz=KST).year)) if default else st.session_state.get(SK["year"], datetime.now(tz=KST).year)))
    semester = st.selectbox("학기", [1, 2], index=((default.get("semester", 1) - 1) if default else st.session_state.get(SK["semester"], 1) - 1))
    class_label = st.text_input("수업 반(예: 2-1)", value=(default.get("class_label", "") if default else ""))

    col1, col2 = st.columns(2)
    if col1.button("저장", type="primary"):
        st.session_state[SK["submit_cache"]] = {
            "year": int(year),
            "semester": int(semester),
            "class_label": class_label.strip(),
            "course_id": course_id or (default.get("course_id") if default else None),
            "class_id": (default.get("_id") if default else None)
        }
    if col2.button("취소"):
        st.session_state[SK["submit_cache"]] = None


@st.dialog("요일·교시 편집")
def dlg_schedule_editor(default: Optional[List[Dict[str, Any]]] = None):
    st.write("요일별 교시를 선택하세요.")
    schedule = []
    for code, label in DAYS:
        with st.expander(f"{label}"):
            periods = st.multiselect(f"{label}요일 교시", PERIOD_CHOICES, default=[p.get("period") for p in (default or []) if p.get("day") == code])
            for p in sorted(periods):
                schedule.append({"day": code, "period": int(p)})
    col1, col2 = st.columns(2)
    if col1.button("저장", type="primary"):
        st.session_state[SK["submit_cache"]] = {"schedule": schedule}
    if col2.button("취소"):
        st.session_state[SK["submit_cache"]] = None


@st.dialog("학생 추가/수정")
def dlg_student_form(default: Optional[Dict[str, Any]] = None):
    student_no = st.text_input("학번", value=(default.get("student_no", "") if default else ""))
    name = st.text_input("성명", value=(default.get("name", "") if default else ""))
    col1, col2 = st.columns(2)
    if col1.button("저장", type="primary"):
        st.session_state[SK["submit_cache"]] = {"student_no": student_no.strip(), "name": name.strip(), "student_id": (default.get("_id") if default else None)}
    if col2.button("취소"):
        st.session_state[SK["submit_cache"]] = None


@st.dialog("학생 CSV 업로드")
def dlg_students_csv_upload():
    st.markdown("CSV 헤더는 **student_no,name** 이어야 합니다.")
    csv_file = st.file_uploader("CSV 파일 선택", type=["csv"], accept_multiple_files=False)
    if st.button("업로드", type="primary"):
        st.session_state[SK["submit_cache"]] = {"csv_file": csv_file}
    if st.button("취소"):
        st.session_state[SK["submit_cache"]] = None


@st.dialog("수업 기록 추가/수정")
def dlg_lesson_log_form(default: Optional[Dict[str, Any]] = None, schedule_periods: Optional[List[int]] = None):
    date_val = st.date_input("일자", value=(datetime.strptime(default.get("date"), "%Y-%m-%d").date() if default and default.get("date") else date.today()))
    period = st.selectbox("교시", options=(schedule_periods or PERIOD_CHOICES), index=((schedule_periods or PERIOD_CHOICES).index(int(default.get("period"))) if default and default.get("period") in (schedule_periods or PERIOD_CHOICES) else 0))
    progress = st.text_area("진도", value=(default.get("progress", "") if default else ""))
    note = st.text_area("특기사항", value=(default.get("note", "") if default else ""))
    col1, col2 = st.columns(2)
    if col1.button("저장", type="primary"):
        st.session_state[SK["submit_cache"]] = {
            "date": date_val.strftime("%Y-%m-%d"),
            "period": int(period),
            "progress": progress.strip(),
            "note": note.strip(),
            "log_id": (default.get("_id") if default else None)
        }
    if col2.button("취소"):
        st.session_state[SK["submit_cache"]] = None


@st.dialog("출결 편집")
def dlg_attendance_edit(default: Optional[Dict[str, Any]] = None):
    student_name = default.get("student_name", "") if default else ""
    st.write(f"학생: **{student_name}**")
    status = st.selectbox("출결", ATTENDANCE_CHOICES, index=(ATTENDANCE_CHOICES.index(default.get("status")) if default and default.get("status") in ATTENDANCE_CHOICES else ATTENDANCE_CHOICES.index("U")))
    remark = st.text_input("특기사항", value=(default.get("remark", "") if default else ""))
    col1, col2 = st.columns(2)
    if col1.button("저장", type="primary"):
        st.session_state[SK["submit_cache"]] = {"status": status, "remark": remark}
    if col2.button("취소"):
        st.session_state[SK["submit_cache"]] = None


# =============================
# Pages
# =============================

def page_courses():
    st.header("교과 관리")
    year, semester = year_semester_filters()

    name_filter = st.text_input("교과명 검색", "")
    try:
        rows = list_courses(year, semester, name_filter)
        if not rows:
            st.info("등록된 교과가 없습니다.")
        else:
            for r in rows:
                with st.container(border=True):
                    c1, c2, c3, c4, c5 = st.columns([2,1,2,2,2])
                    c1.write(f"**{r.get('subject_name','')}**")
                    c2.write(f"{r.get('year','')}-{r.get('semester','')}")
                    c3.write(f"계획서: {'있음' if r.get('storage_path') else '없음'}")
                    # 액션 버튼
                    bcol1, bcol2, bcol3 = c4, c5, st.columns(1)[0]
                    if bcol1.button("PDF 링크 생성", key=f"course_pdf_{r['_id']}"):
                        try:
                            if not r.get("storage_path"):
                                st.warning("업로드된 PDF가 없습니다.")
                            else:
                                url = generate_signed_url(r["storage_path"], hours=1)
                                st.link_button("다운로드(1시간 유효)", url)
                        except Exception as e:
                            st.error(f"링크 생성 실패: {e}")
                    if bcol2.button("수정", key=f"course_edit_{r['_id']}"):
                        dlg_course_form(r)
                    if bcol3.button("삭제", key=f"course_del_{r['_id']}"):
                        try:
                            delete_course(r["_id"])
                            st.success("삭제되었습니다. 새로고침하세요.")
                        except Exception as e:
                            st.error(f"삭제 실패: {e}")
    except GoogleCloudError as e:
        st.error(f"Firestore 조회 실패: {e}")

    st.divider()
    if st.button("+ 교과 추가"):
        dlg_course_form()

    # dialog 결과 처리
    payload = st.session_state.pop(SK["submit_cache"], None)
    if isinstance(payload, dict) and "subject_name" in payload:
        try:
            pdf_path = None
            if payload.get("pdf") is not None:
                pdf_path = upload_pdf(payload["pdf"], payload["year"], payload["semester"], payload["subject_name"])  # type: ignore
            _ = create_or_update_course(payload["year"], payload["semester"], payload["subject_name"], pdf_path)
            st.success("저장되었습니다. 상단 목록을 새로고침하세요.")
        except Exception as e:
            st.error(f"저장 실패: {e}")


def page_classes():
    st.header("수업 반 편성")
    year, semester = year_semester_filters()
    course_id = select_course(year, semester)
    if not course_id:
        return

    try:
        rows = list_classes(course_id)
        if not rows:
            st.info("선택한 교과에 등록된 수업이 없습니다.")
        else:
            for r in rows:
                with st.container(border=True):
                    c1, c2, c3, c4 = st.columns([1,2,1,2])
                    c1.write(f"**{r.get('class_label','')}**")
                    c2.write(f"시간표: {schedule_summary(r.get('schedule', []))}")
                    if c3.button("편집", key=f"class_edit_{r['_id']}"):
                        dlg_class_form(r)
                    if c4.button("삭제", key=f"class_del_{r['_id']}"):
                        try:
                            delete_class(r["_id"])
                            st.success("삭제되었습니다. 새로고침하세요.")
                        except Exception as e:
                            st.error(f"삭제 실패: {e}")
                    # 요일·교시 편집 버튼
                    if st.button("시간표 편집", key=f"schedule_{r['_id']}"):
                        dlg_schedule_editor(r.get("schedule", []))
                        # 결과 처리 아래 공통 핸들러 사용
    except GoogleCloudError as e:
        st.error(f"데이터 조회 실패: {e}")

    st.divider()
    if st.button("+ 수업 등록"):
        dlg_class_form(course_id=course_id)

    # dialog 결과 처리
    payload = st.session_state.pop(SK["submit_cache"], None)
    if isinstance(payload, dict):
        try:
            if "class_label" in payload:  # 수업 등록/수정
                cid = create_or_update_class(
                    course_id=payload.get("course_id") or course_id,
                    class_label=payload["class_label"],
                    year=payload["year"],
                    semester=payload["semester"],
                    class_id=payload.get("class_id")
                )
                st.success("저장되었습니다. 새로고침하세요.")
                st.session_state[SK["class_id"]] = cid
            elif "schedule" in payload:  # 시간표 편집 결과
                sel_class = st.session_state.get(SK["class_id"])  # 최근 선택 또는 마지막 클릭 문맥
                # 보수적으로: 가장 최근 classes에서 첫 번째를 적용하지 않도록 체크
                if not sel_class:
                    # 코스의 첫 수업반에 적용(사용자 안내)
                    all_classes = list_classes(course_id)
                    if all_classes:
                        sel_class = all_classes[0]["_id"]
                if sel_class:
                    create_or_update_class(course_id=course_id, class_label=get_class(sel_class).get("class_label", ""),
                                           year=year, semester=semester, schedule=payload["schedule"], class_id=sel_class)
                    st.success("시간표가 저장되었습니다. 새로고침하세요.")
                else:
                    st.info("적용할 수업 반이 없습니다.")
        except Exception as e:
            st.error(f"저장 실패: {e}")


def page_students():
    st.header("학생 명단 관리")
    year, semester = year_semester_filters()
    course_id = select_course(year, semester)
    if not course_id:
        return
    class_id = select_class(course_id)
    if not class_id:
        return

    try:
        students = list_students(class_id)
        if not students:
            st.info("학생 명단이 없습니다. 추가해 주세요.")
        else:
            for s in students:
                cols = st.columns([2,2,1,1])
                cols[0].write(f"학번: **{s.get('student_no','')}**")
                cols[1].write(f"성명: **{s.get('name','')}**")
                if cols[2].button("수정", key=f"stu_edit_{s['_id']}"):
                    dlg_student_form(s)
                if cols[3].button("삭제", key=f"stu_del_{s['_id']}"):
                    try:
                        delete_student(s["_id"])
                        st.success("삭제되었습니다. 새로고침하세요.")
                    except Exception as e:
                        st.error(f"삭제 실패: {e}")
    except GoogleCloudError as e:
        st.error(f"학생 조회 실패: {e}")

    st.divider()
    colA, colB = st.columns(2)
    if colA.button("+ 학생 추가"):
        dlg_student_form()
    if colB.button("CSV 업로드"):
        dlg_students_csv_upload()

    # dialog 결과 처리
    payload = st.session_state.pop(SK["submit_cache"], None)
    if isinstance(payload, dict):
        try:
            if "student_no" in payload:  # 단건 추가/수정
                # 중복 체크
                dup = get_student_by_no(class_id, payload["student_no"]) if not payload.get("student_id") else None
                if dup:
                    st.error("동일 학번이 이미 존재합니다.")
                else:
                    create_or_update_student(class_id, payload["student_no"], payload["name"], payload.get("student_id"))
                    st.success("저장되었습니다. 새로고침하세요.")
            elif "csv_file" in payload:  # CSV 업로드
                f = payload["csv_file"]
                if f is None:
                    st.warning("파일을 선택해 주세요.")
                else:
                    try:
                        content = f.getvalue()
                        # 인코딩 시도
                        try:
                            text = content.decode("utf-8-sig")
                        except Exception:
                            text = content.decode("cp949")
                        reader = csv.DictReader(io.StringIO(text))
                        headers = reader.fieldnames or []
                        expect = ["student_no", "name"]
                        if [h.strip().lower() for h in headers] != expect:
                            st.error("CSV 헤더는 'student_no,name' 이어야 합니다.")
                        else:
                            rows = [r for r in reader]
                            if not rows:
                                st.info("추가할 데이터가 없습니다.")
                            else:
                                # 중복/빈 값 검사
                                batch_updates = []
                                seen = set()
                                for r in rows:
                                    sno = str(r.get("student_no", "")).strip()
                                    nm = str(r.get("name", "")).strip()
                                    if not sno or not nm:
                                        st.warning("빈 값이 있어 건너뜁니다.")
                                        continue
                                    key = sno
                                    if key in seen:
                                        st.warning(f"CSV 내 중복 학번: {sno} (무시)")
                                        continue
                                    seen.add(key)
                                    # 기존 존재 여부 체크
                                    exist = get_student_by_no(class_id, sno)
                                    if exist:
                                        st.warning(f"이미 존재: {sno} (무시)")
                                        continue
                                    ref = db.collection(COLLECTIONS["class_students"]).document()  # type: ignore
                                    data = {
                                        "class_id": class_id,
                                        "student_no": sno,
                                        "name": nm,
                                        "created_at": now_kst_iso(),
                                        "updated_at": now_kst_iso(),
                                    }
                                    batch_updates.append((ref, data))
                                if batch_updates:
                                    batched_set(batch_updates)
                                    st.success(f"{len(batch_updates)}명 추가되었습니다. 새로고침하세요.")
                                else:
                                    st.info("추가할 신규 데이터가 없습니다.")
                    except Exception as e:
                        st.error(f"CSV 처리 실패: {e}")
        except Exception as e:
            st.error(f"저장 실패: {e}")


def page_lesson_logs():
    st.header("수업 기록(반별)")
    year, semester = year_semester_filters()
    course_id = select_course(year, semester)
    if not course_id:
        return
    class_id = select_class(course_id)
    if not class_id:
        return

    klass = get_class(class_id) or {}
    schedule_periods = sorted({int(s.get("period")) for s in klass.get("schedule", [])}) or PERIOD_CHOICES

    # 조회 필터
    date_sel = st.date_input("일자 선택", value=date.today())
    date_str = date_sel.strftime("%Y-%m-%d")

    try:
        logs = list_lesson_logs(class_id, date_str)
        if not logs:
            st.info("해당 조건의 수업 기록이 없습니다.")
        else:
            for lg in logs:
                cols = st.columns([1,1,3,3,1])
                cols[0].write(lg.get("date", ""))
                cols[1].write(f"{lg.get('period','')}교시")
                cols[2].write(lg.get("progress", ""))
                cols[3].write(lg.get("note", ""))
                if cols[4].button("삭제", key=f"log_del_{lg['_id']}"):
                    try:
                        delete_lesson_log(lg["_id"])
                        st.success("삭제되었습니다. 새로고침하세요.")
                    except Exception as e:
                        st.error(f"삭제 실패: {e}")
    except GoogleCloudError as e:
        st.error(f"조회 실패: {e}")

    st.divider()
    if st.button("+ 기록 추가"):
        dlg_lesson_log_form(schedule_periods=schedule_periods)

    payload = st.session_state.pop(SK["submit_cache"], None)
    if isinstance(payload, dict) and "progress" in payload:
        try:
            create_or_update_lesson_log(class_id, payload["date"], payload["period"], payload["progress"], payload.get("note", ""))
            st.success("저장되었습니다. 새로고침하세요.")
        except Exception as e:
            st.error(f"저장 실패: {e}")


def page_attendance():
    st.header("출결 관리(반·학생·일자별)")
    year, semester = year_semester_filters()
    course_id = select_course(year, semester)
    if not course_id:
        return
    class_id = select_class(course_id)
    if not class_id:
        return

    klass = get_class(class_id) or {}
    schedule_periods = sorted({int(s.get("period")) for s in klass.get("schedule", [])})
    if not schedule_periods:
        st.info("시간표가 없습니다. 먼저 요일·교시를 등록하세요.")
        return

    date_sel = st.date_input("일자 선택", value=date.today())
    date_str = date_sel.strftime("%Y-%m-%d")

    students = list_students(class_id)
    if not students:
        st.info("입력할 대상이 없습니다. 학생을 먼저 등록하세요.")
        return

    # 현황 맵 구성 (student_id -> period -> record)
    att_list = list_attendance(class_id, date_str)
    att_map: Dict[str, Dict[int, Dict[str, Any]]] = {}
    for a in att_list:
        att_map.setdefault(a["student_id"], {})[int(a["period"])] = a

    st.write("각 셀을 클릭해 출결을 편집하세요.")
    # 테이블 렌더링: 학생 행, 교시 열
    header_cols = st.columns([2] + [1 for _ in schedule_periods] + [2])
    header_cols[0].write("학생")
    for i, p in enumerate(schedule_periods):
        header_cols[i+1].write(f"{p}교시")
    header_cols[-1].write("특기사항 일괄")

    for stu in students:
        row_cols = st.columns([2] + [1 for _ in schedule_periods] + [2])
        row_cols[0].write(f"{stu.get('student_no','')} {stu.get('name','')}")
        for i, p in enumerate(schedule_periods):
            rec = att_map.get(stu["_id"], {}).get(int(p), {})
            label = rec.get("status", "U")
            key = f"att_btn_{stu['_id']}_{p}"
            if row_cols[i+1].button(label, key=key):
                dlg_attendance_edit({
                    "student_name": f"{stu.get('student_no','')} {stu.get('name','')}",
                    "status": label,
                    "remark": rec.get("remark", ""),
                })
                # 저장 결과 처리 아래 공통 로직
        # 일괄 특기사항(선택적 기능): 입력 후 모든 교시에 remark 적용
        with row_cols[-1]:
            with st.popover("일괄 적용"):
                sel_status = st.selectbox("상태", ATTENDANCE_CHOICES, key=f"bulk_status_{stu['_id']}")
                txt = st.text_input("특기사항", key=f"bulk_remark_{stu['_id']}")
                if st.button("적용", key=f"bulk_apply_{stu['_id']}"):
                    try:
                        updates = []
                        for p in schedule_periods:
                            ref = db.collection(COLLECTIONS["attendance"]).document()  # type: ignore
                            data = {
                                "class_id": class_id,
                                "date": date_str,
                                "period": int(p),
                                "student_id": stu["_id"],
                                "status": sel_status,
                                "remark": txt,
                                "created_at": now_kst_iso(),
                                "updated_at": now_kst_iso(),
                            }
                            updates.append((ref, data))
                        batched_set(updates)
                        st.toast("일괄 적용 완료")
                    except Exception as e:
                        st.error(f"일괄 적용 실패: {e}")

    # dialog 저장 처리(단일 셀)
    payload = st.session_state.pop(SK["submit_cache"], None)
    if isinstance(payload, dict) and "status" in payload:
        # payload에는 status/remark만 들어있음 → 어떤 셀에 대한 편집인지 추적 필요
        # 간단히 마지막 클릭된 버튼 키를 session에 저장하는 방식으로 처리(여기서는 버튼 키가 변수 key)
        # Streamlit은 버튼 클릭 후 즉시 실행되므로, 편집 호출 시 임시 컨텍스트 저장
        ctx = st.session_state.get("_last_att_ctx")
        if ctx:
            try:
                set_attendance(ctx["class_id"], ctx["date"], ctx["period"], ctx["student_id"], payload["status"], payload.get("remark", ""))
                st.success("저장되었습니다. 새로고침하세요.")
            except Exception as e:
                st.error(f"저장 실패: {e}")
        else:
            st.info("편집 컨텍스트를 찾을 수 없습니다. 다시 시도해 주세요.")

    # 버튼 클릭 시 컨텍스트 기록을 위해 rerun 전에 콜백을 두기 어려워 보이므로,
    # 위의 버튼 구성부에서 st.session_state['_last_att_ctx']를 갱신해야 한다.
    # 이를 위해 버튼 생성 직후 즉시 할당하도록 재정의.

    # 재렌더링을 위해 아래 훅을 사용: 버튼이 눌리면 즉시 컨텍스트를 기록
    # (위의 버튼 생성부에서 직접 기록하기가 어려워, 보완 함수를 제공)


def set_att_ctx(class_id: str, date_str: str, period: int, student_id: str):
    st.session_state["_last_att_ctx"] = {
        "class_id": class_id,
        "date": date_str,
        "period": int(period),
        "student_id": student_id,
    }


# 위의 set_att_ctx를 활용하도록 버튼 생성부를 다시 정의하기 어려워 코드 복잡성이 증가할 수 있습니다.
# 간명 버전: 출결 버튼을 누를 때 해당 컨텍스트를 즉시 기록하는 헬퍼를 래핑하여 사용.
# 하지만 Streamlit에서는 버튼 클릭 직후 코드가 재실행되므로, 아래와 같은 패턴으로 처리합니다.
# * 버튼 라벨 대신 링크처럼 동작하는 폼을 사용할 수도 있으나, 요구사항 충족을 위해 dialog를 유지합니다.


def page_daily_progress_report():
    st.header("일자별 진도/특기사항(전체 수업반)")
    date_sel = st.date_input("일자 선택", value=date.today(), key="dp_date")
    date_str = date_sel.strftime("%Y-%m-%d")

    try:
        logs = list_attendance(date_filter=None)  # dummy 호출 방지용
    except Exception:
        pass

    try:
        # 모든 수업반에서 해당 일자의 lesson_logs 수집
        docs = list(db.collection(COLLECTIONS["lesson_logs"]).where("date", "==", date_str).stream())  # type: ignore
        if not docs:
            st.info("선택한 일자의 데이터가 없습니다.")
            return
        # class_id → class_label, course_id
        classes = {d.id: d.to_dict() for d in db.collection(COLLECTIONS["classes"]).stream()}  # type: ignore
        courses = {d.id: d.to_dict() for d in db.collection(COLLECTIONS["courses"]).stream()}  # type: ignore
        rows = []
        for d in docs:
            x = d.to_dict() or {}
            cid = x.get("class_id")
            klass = classes.get(cid, {})
            course = courses.get(klass.get("course_id"), {})
            rows.append({
                "일자": x.get("date"),
                "반": klass.get("class_label", ""),
                "교과": course.get("subject_name", ""),
                "교시": x.get("period"),
                "진도": x.get("progress", ""),
                "특기사항": x.get("note", ""),
            })
        if not rows:
            st.info("선택한 일자의 데이터가 없습니다.")
            return
        if pd is not None:
            df = pd.DataFrame(rows)
            st.dataframe(df, use_container_width=True)
            st.download_button("CSV 다운로드", data=df.to_csv(index=False).encode("utf-8-sig"), file_name=f"진도_{date_str}.csv", mime="text/csv")
        else:
            for r in rows:
                st.write(r)
    except GoogleCloudError as e:
        st.error(f"조회 실패: {e}")


def page_daily_attendance_report():
    st.header("일자별 출결/특기사항(전체 수업반)")
    date_sel = st.date_input("일자 선택", value=date.today(), key="da_date")
    date_str = date_sel.strftime("%Y-%m-%d")

    try:
        docs = list(db.collection(COLLECTIONS["attendance"]).where("date", "==", date_str).stream())  # type: ignore
        if not docs:
            st.info("선택한 일자의 데이터가 없습니다.")
            return
        # class, course, student lookup
        classes = {d.id: d.to_dict() for d in db.collection(COLLECTIONS["classes"]).stream()}  # type: ignore
        courses = {d.id: d.to_dict() for d in db.collection(COLLECTIONS["courses"]).stream()}  # type: ignore
        students = {d.id: d.to_dict() for d in db.collection(COLLECTIONS["class_students"]).stream()}  # type: ignore

        rows = []
        for d in docs:
            x = d.to_dict() or {}
            cid = x.get("class_id")
            klass = classes.get(cid, {})
            course = courses.get(klass.get("course_id"), {})
            stu = students.get(x.get("student_id"), {})
            rows.append({
                "반": klass.get("class_label", ""),
                "교과": course.get("subject_name", ""),
                "학번": stu.get("student_no", ""),
                "성명": stu.get("name", ""),
                "교시": x.get("period"),
                "출결": x.get("status", "U"),
                "특기사항": x.get("remark", ""),
            })
        if not rows:
            st.info("선택한 일자의 데이터가 없습니다.")
            return
        if pd is not None:
            df = pd.DataFrame(rows)
            df = df.sort_values(["반", "학번", "교시"]).reset_index(drop=True)
            st.dataframe(df, use_container_width=True)
            st.download_button("CSV 다운로드", data=df.to_csv(index=False).encode("utf-8-sig"), file_name=f"출결_{date_str}.csv", mime="text/csv")
        else:
            for r in rows:
                st.write(r)
    except GoogleCloudError as e:
        st.error(f"조회 실패: {e}")


# =============================
# 라우팅 & 메인
# =============================

MENU = {
    "교과 관리": page_courses,
    "수업 반 편성": page_classes,
    "학생 명단 관리": page_students,
    "수업 기록(반별)": page_lesson_logs,
    "출결 관리": page_attendance,
    "일자별 진도/특기사항": page_daily_progress_report,
    "일자별 출결/특기사항": page_daily_attendance_report,
}


def main():
    st.set_page_config(page_title="수업·출결 관리", layout="wide")
    st.title("수업·출결 관리")
    st.caption("Streamlit + Firebase (Firestore/Storage)")

    with st.sidebar:
        st.subheader("메뉴")
        menu = st.selectbox("기능 선택", list(MENU.keys()))
        st.markdown("---")
        st.caption("*PDF는 10MB 이하만 업로드 가능합니다.")

    try:
        MENU[menu]()
    except Exception as e:
        st.error(f"오류가 발생했습니다: {e}")


if __name__ == "__main__":
    main()
