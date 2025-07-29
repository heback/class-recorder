# app.py — Streamlit + Firebase (Firestore, Storage)
# 단일 파일 구현 / 요구사항 충족 버전
# ------------------------------------------------------------
# 기능 요약
# 1) 담당 교과(교과명/학년도/학기) + 수업/평가 계획 PDF 업로드·조회(10MB 제한, PDF만)
# 2) 수업(반) 관리: 학년도/학기/교과 선택, 반명, 요일/교시 등록
# 3) 반별 학생 관리: 개별 추가, CSV 업로드(student_id,name)
# 4) 반별 일자·교시 진도/특기사항 관리
# 5) 반·학생·일자별 출결/특기사항 관리
# 6) 일자별 전체 수업반의 진도/출결 조회
# 7) 데이터 없음 -> st.info, 전 과정 예외 처리
# 8) Streamlit Cloud secrets: FIREBASE_KEY(JSON, storageBucket 포함) 사용
# 9) 모든 입력폼은 st.dialog 사용
# ------------------------------------------------------------

import io
import csv
import json
import time
import base64
import traceback
from datetime import datetime, date
from typing import List, Dict, Any, Optional

import streamlit as st

# Firebase Admin SDK
import firebase_admin
from firebase_admin import credentials, firestore, storage
from google.cloud.exceptions import NotFound

# ------------------------------------------------------------
# 전역 상수/설정
# ------------------------------------------------------------
APP_TITLE = "수업/평가 관리"
MAX_PDF_MB = 10
PDF_MIME = "application/pdf"
ATTENDANCE_STATES = ["출석", "지각", "결석", "조퇴", "미입력"]
WEEKDAYS = ["월", "화", "수", "목", "금", "토", "일"]  # 0~6

# ------------------------------------------------------------
# Firebase 초기화 (Secrets: FIREBASE_KEY)
# ------------------------------------------------------------
@st.cache_resource(show_spinner=False)
def init_firebase():
    """Initialize Firebase using FIREBASE_KEY from Streamlit secrets.
    FIREBASE_KEY must be a JSON that also includes storageBucket.
    Returns Firestore client and Storage bucket.
    """
    raw = st.secrets.get("FIREBASE_KEY")
    if raw is None:
        st.stop()
    try:
        cred_info = json.loads(raw) if isinstance(raw, str) else dict(raw)
        if "storageBucket" not in cred_info:
            st.error("FIREBASE_KEY에 storageBucket이 포함되어야 합니다.")
            st.stop()
        cred = credentials.Certificate(cred_info)
        firebase_admin.initialize_app(cred, {"storageBucket": cred_info.get("storageBucket")})
        return firestore.client(), storage.bucket()
    except Exception as e:
        st.error(f"Firebase 초기화 실패: {e}")
        st.stop()


db, bucket = init_firebase()

# ------------------------------------------------------------
# 유틸리티
# ------------------------------------------------------------

def now_ts():
    return firestore.SERVER_TIMESTAMP


def to_datestr(d: date) -> str:
    return d.strftime("%Y-%m-%d")


def to_docid_yyyymmdd(d: date) -> str:
    return d.strftime("%Y%m%d")


def safe_info_if_empty(rows, msg="데이터가 없습니다"):
    if not rows:
        st.info(msg)
        return True
    return False


def validate_pdf(file) -> Optional[str]:
    """PDF 검증: 확장자, MIME, 크기(≤10MB). 문제 없으면 None 반환, 아니면 에러 메시지 반환."""
    if file is None:
        return None
    name = file.name.lower()
    if not name.endswith(".pdf"):
        return "PDF 파일만 업로드 가능합니다 (.pdf)."
    # Streamlit UploadedFile은 type을 제공할 수 있음 (단, 브라우저에 따라 다름)
    if getattr(file, "type", "") and file.type != PDF_MIME:
        return "잘못된 MIME 타입입니다. PDF만 허용됩니다."
    # size check
    file.seek(0, io.SEEK_END)
    size = file.tell()
    file.seek(0)
    if size > MAX_PDF_MB * 1024 * 1024:
        return f"PDF는 {MAX_PDF_MB}MB 이하만 업로드 가능합니다."
    return None


def generate_signed_url(blob_path: str, minutes: int = 30) -> Optional[str]:
    try:
        blob = bucket.blob(blob_path)
        if not blob.exists():
            return None
        from datetime import timedelta
        url = blob.generate_signed_url(expiration=timedelta(minutes=minutes), method="GET")
        return url
    except Exception:
        return None


def download_blob_bytes(blob_path: str) -> Optional[bytes]:
    try:
        blob = bucket.blob(blob_path)
        return blob.download_as_bytes()
    except Exception:
        return None


def csv_bytes(rows: List[Dict[str, Any]], headers: List[str]) -> bytes:
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=headers)
    writer.writeheader()
    for r in rows:
        writer.writerow({h: r.get(h, "") for h in headers})
    return output.getvalue().encode("utf-8-sig")


# ------------------------------------------------------------
# Firestore 서비스 함수
# ------------------------------------------------------------
# collections: courses, classes, classes/{classId}/students, progress, attendance

# -------------------- Courses --------------------

def list_courses(filters: Dict[str, Any] = None) -> List[Dict[str, Any]]:
    try:
        col = db.collection("courses")
        q = col
        if filters:
            if filters.get("academic_year"):
                q = q.where("academic_year", "==", int(filters["academic_year"]))
            if filters.get("semester"):
                q = q.where("semester", "==", str(filters["semester"]))
        docs = q.order_by("subject_name").stream()
        return [{"id": d.id, **d.to_dict()} for d in docs]
    except Exception as e:
        st.error(f"교과 조회 실패: {e}")
        return []


def get_course(course_id: str) -> Optional[Dict[str, Any]]:
    try:
        d = db.collection("courses").document(course_id).get()
        return {"id": d.id, **d.to_dict()} if d.exists else None
    except Exception as e:
        st.error(f"교과 조회 실패: {e}")
        return None


def create_course(subject_name: str, academic_year: int, semester: str, pdf_file) -> Optional[str]:
    try:
        data = {
            "subject_name": subject_name,
            "academic_year": academic_year,
            "semester": str(semester),
            "plan_pdf_url": None,
            "plan_pdf_name": None,
            "plan_pdf_size": None,
            "storage_path": None,
            "created_at": now_ts(),
            "updated_at": now_ts(),
        }
        ref = db.collection("courses").document()
        ref.set(data)
        # PDF 업로드
        if pdf_file is not None:
            err = validate_pdf(pdf_file)
            if err:
                st.error(err)
            else:
                blob_path = f"courses/{ref.id}/plan.pdf"
                blob = bucket.blob(blob_path)
                blob.upload_from_file(pdf_file, content_type=PDF_MIME)
                size = blob.size
                url = generate_signed_url(blob_path) or ""
                ref.update({
                    "plan_pdf_url": url,
                    "plan_pdf_name": pdf_file.name,
                    "plan_pdf_size": size,
                    "storage_path": blob_path,
                    "updated_at": now_ts(),
                })
        return ref.id
    except Exception as e:
        st.error(f"교과 생성 실패: {e}")
        return None


def update_course(course_id: str, subject_name: str, academic_year: int, semester: str, pdf_file) -> bool:
    try:
        ref = db.collection("courses").document(course_id)
        payload = {
            "subject_name": subject_name,
            "academic_year": academic_year,
            "semester": str(semester),
            "updated_at": now_ts(),
        }
        ref.update(payload)
        if pdf_file is not None:
            err = validate_pdf(pdf_file)
            if err:
                st.error(err)
                return False
            blob_path = f"courses/{course_id}/plan.pdf"
            blob = bucket.blob(blob_path)
            blob.upload_from_file(pdf_file, content_type=PDF_MIME)
            size = blob.size
            url = generate_signed_url(blob_path) or ""
            ref.update({
                "plan_pdf_url": url,
                "plan_pdf_name": pdf_file.name,
                "plan_pdf_size": size,
                "storage_path": blob_path,
                "updated_at": now_ts(),
            })
        return True
    except Exception as e:
        st.error(f"교과 수정 실패: {e}")
        return False


def delete_course(course_id: str) -> bool:
    try:
        # 연관 classes 존재 확인
        classes = db.collection("classes").where("course_ref", "==", db.collection("courses").document(course_id)).limit(1).stream()
        if any(True for _ in classes):
            st.warning("해당 교과와 연결된 수업(반)이 있어 삭제할 수 없습니다.")
            return False
        # Storage 파일 삭제
        blob_path = f"courses/{course_id}/plan.pdf"
        blob = bucket.blob(blob_path)
        try:
            blob.delete()
        except Exception:
            pass
        db.collection("courses").document(course_id).delete()
        return True
    except Exception as e:
        st.error(f"교과 삭제 실패: {e}")
        return False


# -------------------- Classes --------------------

def list_classes(filters: Dict[str, Any] = None) -> List[Dict[str, Any]]:
    try:
        q = db.collection("classes")
        if filters:
            if filters.get("academic_year"):
                q = q.where("academic_year", "==", int(filters["academic_year"]))
            if filters.get("semester"):
                q = q.where("semester", "==", str(filters["semester"]))
            if filters.get("course_id"):
                q = q.where("course_ref", "==", db.collection("courses").document(filters["course_id"]))
        docs = q.order_by("class_name").stream()
        res = []
        for d in docs:
            data = d.to_dict()
            # denormalize course name for view
            course_name = None
            try:
                course = data.get("course_ref").get().to_dict() if data.get("course_ref") else None
                if course:
                    course_name = course.get("subject_name")
            except Exception:
                pass
            res.append({"id": d.id, **data, "course_name": course_name})
        return res
    except Exception as e:
        st.error(f"수업(반) 조회 실패: {e}")
        return []


def get_class(class_id: str) -> Optional[Dict[str, Any]]:
    try:
        d = db.collection("classes").document(class_id).get()
        return {"id": d.id, **d.to_dict()} if d.exists else None
    except Exception as e:
        st.error(f"수업(반) 조회 실패: {e}")
        return None


def is_class_name_duplicate(academic_year: int, semester: str, class_name: str) -> bool:
    try:
        q = db.collection("classes").where("academic_year", "==", int(academic_year)).where("semester", "==", str(semester)).where("class_name", "==", class_name).limit(1)
        return any(True for _ in q.stream())
    except Exception:
        return False


def create_class(course_id: str, class_name: str, academic_year: int, semester: str, schedule: List[Dict[str, int]]) -> Optional[str]:
    try:
        if is_class_name_duplicate(academic_year, semester, class_name):
            st.error("동일 학년도·학기의 동일 반명이 이미 존재합니다.")
            return None
        data = {
            "course_ref": db.collection("courses").document(course_id) if course_id else None,
            "class_name": class_name,
            "academic_year": academic_year,
            "semester": str(semester),
            "schedule": schedule,
            "created_at": now_ts(),
            "updated_at": now_ts(),
        }
        ref = db.collection("classes").document()
        ref.set(data)
        return ref.id
    except Exception as e:
        st.error(f"수업(반) 생성 실패: {e}")
        return None


def update_class(class_id: str, course_id: str, class_name: str, academic_year: int, semester: str, schedule: List[Dict[str, int]]) -> bool:
    try:
        payload = {
            "course_ref": db.collection("courses").document(course_id) if course_id else None,
            "class_name": class_name,
            "academic_year": academic_year,
            "semester": str(semester),
            "schedule": schedule,
            "updated_at": now_ts(),
        }
        db.collection("classes").document(class_id).update(payload)
        return True
    except Exception as e:
        st.error(f"수업(반) 수정 실패: {e}")
        return False


def delete_class(class_id: str) -> bool:
    try:
        # 하위 서브컬렉션 삭제 경고: 여기서는 하위 문서 존재해도 삭제 가능하도록 예시, 실제 운영에서는 보호 권장
        # students/progress/attendance 삭제
        class_ref = db.collection("classes").document(class_id)
        # students
        for d in class_ref.collection("students").stream():
            d.reference.delete()
        # progress
        for d in class_ref.collection("progress").stream():
            d.reference.delete()
        # attendance
        for d in class_ref.collection("attendance").stream():
            d.reference.delete()
        # 본문서 삭제
        class_ref.delete()
        return True
    except Exception as e:
        st.error(f"수업(반) 삭제 실패: {e}")
        return False


# -------------------- Students --------------------

def list_students(class_id: str) -> List[Dict[str, Any]]:
    try:
        docs = db.collection("classes").document(class_id).collection("students").order_by("student_id").stream()
        return [{"id": d.id, **d.to_dict()} for d in docs]
    except Exception as e:
        st.error(f"학생 조회 실패: {e}")
        return []


def is_student_id_duplicate(class_id: str, student_id: str) -> bool:
    try:
        q = db.collection("classes").document(class_id).collection("students").where("student_id", "==", student_id).limit(1)
        return any(True for _ in q.stream())
    except Exception:
        return False


def add_student(class_id: str, student_id: str, name: str) -> Optional[str]:
    try:
        if is_student_id_duplicate(class_id, student_id):
            st.error("이미 존재하는 학번입니다.")
            return None
        data = {
            "student_id": str(student_id),
            "name": name,
            "created_at": now_ts(),
            "updated_at": now_ts(),
        }
        ref = db.collection("classes").document(class_id).collection("students").document()
        ref.set(data)
        return ref.id
    except Exception as e:
        st.error(f"학생 추가 실패: {e}")
        return None


def update_student(class_id: str, student_doc_id: str, name: str, student_id: str) -> bool:
    try:
        payload = {"name": name, "student_id": str(student_id), "updated_at": now_ts()}
        db.collection("classes").document(class_id).collection("students").document(student_doc_id).update(payload)
        return True
    except Exception as e:
        st.error(f"학생 수정 실패: {e}")
        return False


def delete_student(class_id: str, student_doc_id: str) -> bool:
    try:
        db.collection("classes").document(class_id).collection("students").document(student_doc_id).delete()
        return True
    except Exception as e:
        st.error(f"학생 삭제 실패: {e}")
        return False


def parse_students_csv(file) -> Dict[str, Any]:
    """CSV 파싱 및 검증. 헤더: student_id,name. 결과 dict에 success, errors, rows 리턴."""
    result = {"success": False, "errors": [], "rows": []}
    try:
        text = file.read().decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text))
        expected = ["student_id", "name"]
        if reader.fieldnames is None or [h.strip() for h in reader.fieldnames] != expected:
            result["errors"].append("CSV 헤더는 'student_id,name' 이어야 합니다.")
            return result
        seen = set()
        for i, row in enumerate(reader, start=2):  # include header row index=1
            sid = str(row.get("student_id", "")).strip()
            nm = str(row.get("name", "")).strip()
            if not sid or not nm:
                result["errors"].append(f"{i}행: 학번/성명 누락")
                continue
            if sid in seen:
                result["errors"].append(f"{i}행: 학번 중복")
                continue
            seen.add(sid)
            result["rows"].append({"student_id": sid, "name": nm})
            if len(result["rows"]) > 500:
                result["errors"].append("최대 500명까지 업로드 가능합니다.")
                break
        result["success"] = len(result["rows"]) > 0 and len(result["errors"]) == 0
        return result
    except Exception as e:
        result["errors"].append(f"CSV 파싱 오류: {e}")
        return result


def bulk_upsert_students(class_id: str, rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    res = {"inserted": 0, "updated": 0, "skipped": 0}
    try:
        students_ref = db.collection("classes").document(class_id).collection("students")
        # 기존 학번 인덱스
        existing = {d.to_dict().get("student_id"): d.id for d in students_ref.stream()}
        batch = db.batch()
        count = 0
        for r in rows:
            sid, nm = r.get("student_id"), r.get("name")
            if not sid or not nm:
                res["skipped"] += 1
                continue
            if sid in existing:
                doc_ref = students_ref.document(existing[sid])
                batch.update(doc_ref, {"name": nm, "updated_at": now_ts()})
                res["updated"] += 1
            else:
                doc_ref = students_ref.document()
                batch.set(doc_ref, {"student_id": sid, "name": nm, "created_at": now_ts(), "updated_at": now_ts()})
                res["inserted"] += 1
            count += 1
            if count % 400 == 0:  # Firestore 배치 제한 고려
                batch.commit()
                batch = db.batch()
        batch.commit()
        return res
    except Exception as e:
        st.error(f"학생 일괄 업서트 실패: {e}")
        return res


# -------------------- Progress (수업 일지) --------------------

def get_progress_doc(class_id: str, d: date) -> Optional[Dict[str, Any]]:
    doc_id = to_docid_yyyymmdd(d)
    try:
        doc = db.collection("classes").document(class_id).collection("progress").document(doc_id).get()
        return doc.to_dict() if doc.exists else None
    except Exception:
        return None


def upsert_progress(class_id: str, d: date, items: List[Dict[str, Any]], class_name: str):
    doc_id = to_docid_yyyymmdd(d)
    try:
        ref = db.collection("classes").document(class_id).collection("progress").document(doc_id)
        ref.set({
            "date": to_datestr(d),
            "items": items,
            "class_name": class_name,
            "updated_at": now_ts(),
        }, merge=True)
        return True
    except Exception as e:
        st.error(f"진도 저장 실패: {e}")
        return False


def delete_progress(class_id: str, d: date, period: Optional[int] = None):
    doc_id = to_docid_yyyymmdd(d)
    try:
        ref = db.collection("classes").document(class_id).collection("progress").document(doc_id)
        snap = ref.get()
        if not snap.exists:
            return True
        data = snap.to_dict()
        if period is None:
            ref.delete()
        else:
            items = [it for it in data.get("items", []) if it.get("period") != period]
            ref.update({"items": items, "updated_at": now_ts()})
        return True
    except Exception as e:
        st.error(f"진도 삭제 실패: {e}")
        return False


# -------------------- Attendance (출결) --------------------

def get_attendance_doc(class_id: str, d: date) -> Optional[Dict[str, Any]]:
    doc_id = to_docid_yyyymmdd(d)
    try:
        doc = db.collection("classes").document(class_id).collection("attendance").document(doc_id).get()
        return doc.to_dict() if doc.exists else None
    except Exception:
        return None


def upsert_attendance(class_id: str, d: date, students_map: Dict[str, Dict[str, Any]], class_name: str):
    doc_id = to_docid_yyyymmdd(d)
    try:
        ref = db.collection("classes").document(class_id).collection("attendance").document(doc_id)
        ref.set({
            "date": to_datestr(d),
            "students": students_map,
            "class_name": class_name,
            "updated_at": now_ts(),
        }, merge=True)
        return True
    except Exception as e:
        st.error(f"출결 저장 실패: {e}")
        return False


def delete_attendance(class_id: str, d: date, student_doc_id: Optional[str] = None):
    doc_id = to_docid_yyyymmdd(d)
    try:
        ref = db.collection("classes").document(class_id).collection("attendance").document(doc_id)
        snap = ref.get()
        if not snap.exists:
            return True
        data = snap.to_dict()
        if student_doc_id is None:
            ref.delete()
        else:
            students = data.get("students", {})
            if student_doc_id in students:
                students.pop(student_doc_id, None)
                ref.update({"students": students, "updated_at": now_ts()})
        return True
    except Exception as e:
        st.error(f"출결 삭제 실패: {e}")
        return False


# -------------------- Collection Group Queries (일자별 전체 조회) --------------------

def query_progress_by_date(d: date) -> List[Dict[str, Any]]:
    datestr = to_datestr(d)
    try:
        docs = db.collection_group("progress").where("date", "==", datestr).stream()
        res = []
        for doc in docs:
            data = doc.to_dict()
            res.append({"class_name": data.get("class_name"), "items": data.get("items", [])})
        return res
    except Exception as e:
        st.error(f"진도 조회 실패: {e}")
        return []


def query_attendance_by_date(d: date) -> List[Dict[str, Any]]:
    datestr = to_datestr(d)
    try:
        docs = db.collection_group("attendance").where("date", "==", datestr).stream()
        res = []
        for doc in docs:
            data = doc.to_dict()
            res.append({"class_name": data.get("class_name"), "students": data.get("students", {})})
        return res
    except Exception as e:
        st.error(f"출결 조회 실패: {e}")
        return []


# ------------------------------------------------------------
# Dialog Helpers (st.dialog)
# ------------------------------------------------------------
# Streamlit의 dialog는 함수 데코레이터 사용. 버튼으로 open/close.

@st.dialog("교과 등록/수정", width="large")
def course_dialog(mode: str, course: Optional[Dict[str, Any]] = None):
    try:
        subject_name = st.text_input("교과명", value=(course.get("subject_name") if course else ""))
        col1, col2 = st.columns(2)
        with col1:
            academic_year = st.number_input("학년도", min_value=2000, max_value=2100, step=1, value=int(course.get("academic_year", datetime.now().year)) if course else datetime.now().year)
        with col2:
            semester = st.selectbox("학기", options=["1", "2"], index=(0 if (course and str(course.get("semester")) == "1") else 1 if (course and str(course.get("semester")) == "2") else 0))
        pdf_file = st.file_uploader("수업계획 및 평가계획서 (PDF ≤ 10MB)", type=["pdf"], accept_multiple_files=False)

        btn_cols = st.columns(2)
        with btn_cols[0]:
            if st.button("저장", type="primary"):
                if not subject_name:
                    st.error("교과명을 입력하세요.")
                    return
                if mode == "create":
                    cid = create_course(subject_name, int(academic_year), str(semester), pdf_file)
                    if cid:
                        st.success("교과가 등록되었습니다.")
                        st.session_state["__refresh_courses__"] = True
                        st.rerun()
                else:
                    ok = update_course(course["id"], subject_name, int(academic_year), str(semester), pdf_file)
                    if ok:
                        st.success("교과가 수정되었습니다.")
                        st.session_state["__refresh_courses__"] = True
                        st.rerun()
        with btn_cols[1]:
            st.button("닫기", on_click=lambda: st.session_state.update({"__dialog_open__": False}))
    except Exception as e:
        st.error(f"오류: {e}")


@st.dialog("수업(반) 등록/수정", width="large")
def class_dialog(mode: str, all_courses: List[Dict[str, Any]], cls: Optional[Dict[str, Any]] = None):
    try:
        course_opts = ["(미지정)"] + [f"{c['subject_name']} ({c['academic_year']}-{c['semester']}) | {c['id']}" for c in all_courses]
        default_index = 0
        selected_course_id = None
        if cls and cls.get("course_ref"):
            try:
                cr = cls["course_ref"].get()
                cid = cr.id
                for idx, opt in enumerate(course_opts):
                    if opt.endswith(f"| {cid}"):
                        default_index = idx
                        break
            except Exception:
                pass
        sel = st.selectbox("교과 선택", options=course_opts, index=default_index)
        if "|" in sel:
            selected_course_id = sel.split("|")[-1].strip()

        col1, col2, col3 = st.columns(3)
        with col1:
            class_name = st.text_input("반명", value=(cls.get("class_name") if cls else ""))
        with col2:
            academic_year = st.number_input("학년도", min_value=2000, max_value=2100, step=1, value=int(cls.get("academic_year", datetime.now().year)) if cls else datetime.now().year)
        with col3:
            semester = st.selectbox("학기", options=["1", "2"], index=(0 if (cls and str(cls.get("semester")) == "1") else 1 if (cls and str(cls.get("semester")) == "2") else 0))

        st.markdown("**요일·교시 등록**")
        # schedule builder
        sched_items = []
        existing = cls.get("schedule", []) if cls else []
        n_rows = st.number_input("시간표 행 수", min_value=max(1, len(existing)), max_value=30, value=max(1, len(existing)) or 1)
        for i in range(int(n_rows)):
            wcol, pcol = st.columns(2)
            with wcol:
                default_w = existing[i]["weekday"] if i < len(existing) else 0
                weekday = st.selectbox(f"요일 #{i+1}", options=list(range(7)), format_func=lambda x: WEEKDAYS[x], index=default_w, key=f"weekday_{i}")
            with pcol:
                default_p = existing[i]["period"] if i < len(existing) else 1
                period = st.number_input(f"교시 #{i+1}", min_value=1, max_value=12, step=1, value=default_p, key=f"period_{i}")
            sched_items.append({"weekday": int(weekday), "period": int(period)})

        btn_cols = st.columns(2)
        with btn_cols[0]:
            if st.button("저장", type="primary"):
                if not class_name:
                    st.error("반명을 입력하세요.")
                    return
                if mode == "create":
                    cid = create_class(selected_course_id, class_name, int(academic_year), str(semester), sched_items)
                    if cid:
                        st.success("수업(반)이 등록되었습니다.")
                        st.session_state["__refresh_classes__"] = True
                        st.rerun()
                else:
                    ok = update_class(cls["id"], selected_course_id, class_name, int(academic_year), str(semester), sched_items)
                    if ok:
                        st.success("수업(반)이 수정되었습니다.")
                        st.session_state["__refresh_classes__"] = True
                        st.rerun()
        with btn_cols[1]:
            st.button("닫기", on_click=lambda: st.session_state.update({"__dialog_open__": False}))
    except Exception as e:
        st.error(f"오류: {e}")


@st.dialog("학생 개별 추가", width="large")
def student_dialog_add(class_id: str):
    try:
        sid = st.text_input("학번")
        name = st.text_input("성명")
        cols = st.columns(2)
        with cols[0]:
            if st.button("추가", type="primary"):
                if not sid or not name:
                    st.error("학번과 성명을 입력하세요.")
                    return
                doc_id = add_student(class_id, sid.strip(), name.strip())
                if doc_id:
                    st.success("학생이 추가되었습니다.")
                    st.session_state["__refresh_students__"] = True
                    st.rerun()
        with cols[1]:
            st.button("닫기", on_click=lambda: st.session_state.update({"__dialog_open__": False}))
    except Exception as e:
        st.error(f"오류: {e}")


@st.dialog("학생 CSV 업로드", width="large")
def student_dialog_csv(class_id: str):
    try:
        st.markdown("CSV 헤더는 `student_id,name` 이어야 합니다.")
        csv_template = "student_id,name\n2025001,홍길동\n2025002,김영희\n"
        st.download_button("CSV 템플릿 다운로드", data=csv_template.encode("utf-8-sig"), file_name="students_template.csv")
        file = st.file_uploader("CSV 파일 선택", type=["csv"], accept_multiple_files=False)
        if st.button("업로드", type="primary"):
            if not file:
                st.error("CSV 파일을 선택하세요.")
                return
            res = parse_students_csv(file)
            if res["errors"]:
                for er in res["errors"]:
                    st.error(er)
            if res["rows"]:
                summary = bulk_upsert_students(class_id, res["rows"])
                st.success(f"추가: {summary['inserted']} | 수정: {summary['updated']} | 건너뜀: {summary['skipped']}")
                st.session_state["__refresh_students__"] = True
                st.rerun()
        st.button("닫기", on_click=lambda: st.session_state.update({"__dialog_open__": False}))
    except Exception as e:
        st.error(f"오류: {e}")


@st.dialog("수업 일지 입력", width="large")
def progress_dialog_edit(class_id: str, class_name: str):
    try:
        the_date = st.date_input("일자", value=date.today())
        # 기존 문서 불러오기
        existing = get_progress_doc(class_id, the_date) or {}
        exist_items = existing.get("items", [])
        n = st.number_input("입력 교시 수", min_value=max(1, len(exist_items)), max_value=20, value=max(1, len(exist_items)) or 1)
        new_items = []
        for i in range(int(n)):
            c1, c2 = st.columns([1, 3])
            default_period = exist_items[i]["period"] if i < len(exist_items) else 1
            default_content = exist_items[i]["content"] if i < len(exist_items) else ""
            default_note = exist_items[i].get("note", "") if i < len(exist_items) else ""
            with c1:
                period = st.number_input(f"교시 #{i+1}", min_value=1, max_value=12, value=default_period, step=1, key=f"pg_period_{i}")
            with c2:
                content = st.text_input(f"진도 내용 #{i+1}", value=default_content, key=f"pg_content_{i}")
            note = st.text_input(f"특기사항 #{i+1}", value=default_note, key=f"pg_note_{i}")
            if content:
                new_items.append({"period": int(period), "content": content, "note": note})
        cols = st.columns(2)
        with cols[0]:
            if st.button("저장", type="primary"):
                # 중복 교시 제거/병합
                merged = {}
                for it in new_items:
                    merged[it["period"]] = it
                items = sorted(list(merged.values()), key=lambda x: x["period"])
                if upsert_progress(class_id, the_date, items, class_name):
                    st.success("수업 일지가 저장되었습니다.")
                    st.session_state["__refresh_progress__"] = True
                    st.rerun()
        with cols[1]:
            st.button("닫기", on_click=lambda: st.session_state.update({"__dialog_open__": False}))
    except Exception as e:
        st.error(f"오류: {e}")


@st.dialog("출결 입력", width="large")
def attendance_dialog_edit(class_id: str, class_name: str):
    try:
        the_date = st.date_input("일자", value=date.today())
        # 학생 목록
        students = list_students(class_id)
        if safe_info_if_empty(students, "학생이 없습니다. 먼저 학생을 등록하세요."):
            return
        # 기존 출결 문서
        existing = get_attendance_doc(class_id, the_date) or {}
        students_map = existing.get("students", {})

        st.markdown("**일괄 상태 지정**")
        bulk_state = st.selectbox("상태", ATTENDANCE_STATES, index=ATTENDANCE_STATES.index("미입력"))
        if st.button("전체 적용"):
            for s in students:
                students_map.setdefault(s["id"], {})["status"] = bulk_state

        st.divider()
        st.markdown("**학생별 입력**")
        for s in students:
            sid = s["id"]
            left, right = st.columns([1, 3])
            with left:
                st.write(f"{s['student_id']} {s['name']}")
            with right:
                status = st.selectbox("상태", ATTENDANCE_STATES,
                                      index=ATTENDANCE_STATES.index(students_map.get(sid, {}).get("status", "미입력")),
                                      key=f"att_status_{sid}")
                note = st.text_input("특기사항", value=students_map.get(sid, {}).get("note", ""), key=f"att_note_{sid}")
                students_map[sid] = {"status": status, "note": note}

        cols = st.columns(2)
        with cols[0]:
            if st.button("저장", type="primary"):
                if upsert_attendance(class_id, the_date, students_map, class_name):
                    st.success("출결이 저장되었습니다.")
                    st.session_state["__refresh_attendance__"] = True
                    st.rerun()
        with cols[1]:
            st.button("닫기", on_click=lambda: st.session_state.update({"__dialog_open__": False}))
    except Exception as e:
        st.error(f"오류: {e}")


# ------------------------------------------------------------
# View Functions
# ------------------------------------------------------------

def dashboard_view():
    st.subheader("대시보드")
    pick_date = st.date_input("기준 일자", value=date.today(), key="dash_date")

    # KPI
    try:
        num_courses = len(list_courses())
        num_classes = len(list_classes())
        # 학생 수: 모든 classes의 students 집계
        students_total = 0
        for cls in list_classes():
            students_total += len(list_students(cls["id"]))
    except Exception:
        num_courses = num_classes = students_total = 0

    k1, k2, k3 = st.columns(3)
    k1.metric("교과 수", num_courses)
    k2.metric("수업(반) 수", num_classes)
    k3.metric("학생 수", students_total)

    # 당일 진도/출결 요약
    try:
        prog = query_progress_by_date(pick_date)
        attn = query_attendance_by_date(pick_date)
        st.markdown("### 진도 요약")
        if safe_info_if_empty(prog, "진도 데이터가 없습니다") is False:
            for p in prog:
                st.write(f"**{p['class_name']}**")
                items = p.get("items", [])
                if not items:
                    st.info("데이터가 없습니다")
                for it in items:
                    st.write(f"- {it.get('period')}교시: {it.get('content')}  ")
        st.markdown("### 출결 요약")
        if safe_info_if_empty(attn, "출결 데이터가 없습니다") is False:
            for a in attn:
                st.write(f"**{a['class_name']}**")
                students_map = a.get("students", {})
                counts = {k: 0 for k in ATTENDANCE_STATES}
                for v in students_map.values():
                    s = v.get("status", "미입력")
                    counts[s] = counts.get(s, 0) + 1
                st.write(", ".join([f"{k}:{v}" for k, v in counts.items()]))
    except Exception as e:
        st.error(f"대시보드 로드 실패: {e}")

    st.divider()
    c1, c2 = st.columns(2)
    with c1:
        if st.button("오늘 진도 입력", use_container_width=True):
            st.session_state["__open_progress__"] = True
    with c2:
        if st.button("오늘 출결 입력", use_container_width=True):
            st.session_state["__open_attendance__"] = True


def course_view():
    st.subheader("교과 관리")
    colf1, colf2, colbtn = st.columns([1, 1, 1])
    with colf1:
        fy = st.text_input("학년도 필터", placeholder="예: 2025")
    with colf2:
        fs = st.selectbox("학기 필터", options=["", "1", "2"], index=0)
    with colbtn:
        if st.button("교과 등록", type="primary"):
            course_dialog("create")

    filters = {}
    if fy.strip():
        try:
            filters["academic_year"] = int(fy)
        except Exception:
            st.warning("학년도는 숫자여야 합니다.")
    if fs:
        filters["semester"] = fs

    rows = list_courses(filters)
    if safe_info_if_empty(rows):
        return

    for row in rows:
        with st.expander(f"{row['subject_name']} ({row['academic_year']}-{row['semester']})"):
            c1, c2, c3, c4 = st.columns([2, 1, 1, 1])
            with c1:
                st.write(f"PDF: {'있음' if row.get('storage_path') else '없음'}")
                if row.get("storage_path"):
                    url = generate_signed_url(row["storage_path"]) or ""
                    if url:
                        st.markdown(f"[PDF 열기]({url})")
                    else:
                        bytes_ = download_blob_bytes(row["storage_path"]) or b""
                        if bytes_:
                            st.download_button("PDF 다운로드", data=bytes_, file_name=row.get("plan_pdf_name") or "plan.pdf")
            with c2:
                if st.button("수정", key=f"edit_course_{row['id']}"):
                    course_dialog("update", row)
            with c3:
                if st.button("삭제", key=f"del_course_{row['id']}"):
                    if delete_course(row["id"]):
                        st.success("삭제되었습니다")
                        st.rerun()
            with c4:
                pdf_file = st.file_uploader("PDF 교체", type=["pdf"], key=f"pdf_replace_{row['id']}")
                if pdf_file is not None:
                    ok = update_course(row["id"], row["subject_name"], int(row["academic_year"]), str(row["semester"]), pdf_file)
                    if ok:
                        st.success("PDF가 업로드되었습니다.")
                        st.rerun()


def class_view():
    st.subheader("수업(반) 관리")
    all_courses = list_courses()

    colf1, colf2, colf3, colbtn = st.columns([1, 1, 1, 1])
    with colf1:
        fy = st.text_input("학년도 필터")
    with colf2:
        fs = st.selectbox("학기 필터", options=["", "1", "2"], index=0)
    with colf3:
        course_opt = [""] + [f"{c['subject_name']} | {c['id']}" for c in all_courses]
        sel = st.selectbox("교과 필터", options=course_opt)
        cid_filter = sel.split("|")[-1].strip() if "|" in sel and sel.strip() else None
    with colbtn:
        if st.button("수업(반) 등록", type="primary"):
            class_dialog("create", all_courses)

    filters = {}
    if fy.strip():
        try:
            filters["academic_year"] = int(fy)
        except Exception:
            st.warning("학년도는 숫자여야 합니다.")
    if fs:
        filters["semester"] = fs
    if cid_filter:
        filters["course_id"] = cid_filter

    rows = list_classes(filters)
    if safe_info_if_empty(rows):
        return

    for row in rows:
        schedule_str = ", ".join([f"{WEEKDAYS[it['weekday']]}{it['period']}" for it in row.get("schedule", [])]) or "-"
        with st.expander(f"{row['class_name']} / {row.get('course_name') or '-'} ({row['academic_year']}-{row['semester']})"):
            st.write(f"요일·교시: {schedule_str}")
            c1, c2, c3 = st.columns(3)
            with c1:
                if st.button("수정", key=f"edit_class_{row['id']}"):
                    class_dialog("update", all_courses, row)
            with c2:
                if st.button("삭제", key=f"del_class_{row['id']}"):
                    if delete_class(row["id"]):
                        st.success("삭제되었습니다")
                        st.rerun()
            with c3:
                st.write(" ")


def student_view():
    st.subheader("학생 관리 (반별)")
    classes = list_classes()
    if safe_info_if_empty(classes, "수업(반)이 없습니다. 먼저 수업(반)을 등록하세요."):
        return

    sel = st.selectbox("반 선택", options=[f"{c['class_name']} ({c['academic_year']}-{c['semester']}) | {c['id']}" for c in classes])
    class_id = sel.split("|")[-1].strip()
    st.session_state["__current_class_id__"] = class_id

    st.markdown("### 학생 목록")
    students = list_students(class_id)
    if safe_info_if_empty(students, "학생이 없습니다") is False:
        for s in students:
            c1, c2, c3, c4 = st.columns([1, 1, 2, 1])
            with c1:
                st.write(s.get("student_id"))
            with c2:
                st.write(s.get("name"))
            with c3:
                new_name = st.text_input("성명 수정", value=s.get("name"), key=f"st_name_{s['id']}")
            with c4:
                new_sid = st.text_input("학번 수정", value=str(s.get("student_id")), key=f"st_sid_{s['id']}")
            cc1, cc2 = st.columns(2)
            with cc1:
                if st.button("저장", key=f"st_save_{s['id']}"):
                    ok = update_student(class_id, s["id"], new_name.strip(), new_sid.strip())
                    if ok:
                        st.success("학생 정보가 수정되었습니다.")
                        st.rerun()
            with cc2:
                if st.button("삭제", key=f"st_del_{s['id']}"):
                    if delete_student(class_id, s["id"]):
                        st.success("삭제되었습니다")
                        st.rerun()

    cbtn1, cbtn2 = st.columns(2)
    with cbtn1:
        if st.button("학생 개별 추가", type="primary"):
            student_dialog_add(class_id)
    with cbtn2:
        if st.button("CSV 업로드", type="primary"):
            student_dialog_csv(class_id)


def progress_view():
    st.subheader("수업 일지 (진도·특기사항)")
    classes = list_classes()
    if safe_info_if_empty(classes, "수업(반)이 없습니다. 먼저 수업(반)을 등록하세요."):
        return
    sel = st.selectbox("반 선택", options=[f"{c['class_name']} ({c['academic_year']}-{c['semester']}) | {c['id']}" for c in classes])
    class_id = sel.split("|")[-1].strip()
    cls = get_class(class_id) or {}
    class_name = cls.get("class_name", "")

    col = st.columns(2)
    if st.button("일지 입력/수정", type="primary"):
        progress_dialog_edit(class_id, class_name)

    # 최근 14일 표시
    st.markdown("### 최근 기록")
    try:
        docs = db.collection("classes").document(class_id).collection("progress").order_by("date", direction=firestore.Query.DESCENDING).limit(14).stream()
        rows = []
        for d in docs:
            data = d.to_dict()
            for it in data.get("items", []) if data else []:
                rows.append({
                    "일자": data.get("date"),
                    "교시": it.get("period"),
                    "진도": it.get("content"),
                    "특기": it.get("note", ""),
                })
        if safe_info_if_empty(rows):
            return
        # 간단 표
        st.dataframe(rows, use_container_width=True)
    except Exception as e:
        st.error(f"기록 조회 실패: {e}")


def attendance_view():
    st.subheader("출결 관리 (반·학생·일자별)")
    classes = list_classes()
    if safe_info_if_empty(classes, "수업(반)이 없습니다. 먼저 수업(반)을 등록하세요."):
        return
    sel = st.selectbox("반 선택", options=[f"{c['class_name']} ({c['academic_year']}-{c['semester']}) | {c['id']}" for c in classes])
    class_id = sel.split("|")[-1].strip()
    cls = get_class(class_id) or {}
    class_name = cls.get("class_name", "")

    if st.button("출결 입력/수정", type="primary"):
        attendance_dialog_edit(class_id, class_name)

    # 최근 7일 요약
    st.markdown("### 최근 출결 요약")
    try:
        docs = db.collection("classes").document(class_id).collection("attendance").order_by("date", direction=firestore.Query.DESCENDING).limit(7).stream()
        rows = []
        for d in docs:
            data = d.to_dict()
            counts = {k: 0 for k in ATTENDANCE_STATES}
            for v in data.get("students", {}).values():
                counts[v.get("status", "미입력")] = counts.get(v.get("status", "미입력"), 0) + 1
            rows.append({"일자": data.get("date"), **counts})
        if safe_info_if_empty(rows):
            return
        st.dataframe(rows, use_container_width=True)
    except Exception as e:
        st.error(f"출결 조회 실패: {e}")


def daily_overview_view():
    st.subheader("일자별 전체 조회")
    the_date = st.date_input("일자", value=date.today())

    st.markdown("### 진도 종합")
    prog = query_progress_by_date(the_date)
    if safe_info_if_empty(prog, "진도 데이터가 없습니다") is False:
        rows = []
        for p in prog:
            for it in p.get("items", []):
                rows.append({"반": p.get("class_name"), "교시": it.get("period"), "진도": it.get("content"), "특기": it.get("note", "")})
        st.dataframe(rows, use_container_width=True)
        st.download_button("진도 CSV 다운로드", data=csv_bytes(rows, ["반", "교시", "진도", "특기"]), file_name=f"progress_{to_docid_yyyymmdd(the_date)}.csv")

    st.markdown("### 출결 종합")
    attn = query_attendance_by_date(the_date)
    if safe_info_if_empty(attn, "출결 데이터가 없습니다") is False:
        rows = []
        for a in attn:
            students_map = a.get("students", {})
            for sid, v in students_map.items():
                rows.append({"반": a.get("class_name"), "학생DocID": sid, "상태": v.get("status", "미입력"), "특기": v.get("note", "")})
        st.dataframe(rows, use_container_width=True)
        st.download_button("출결 CSV 다운로드", data=csv_bytes(rows, ["반", "학생DocID", "상태", "특기"]), file_name=f"attendance_{to_docid_yyyymmdd(the_date)}.csv")


# ------------------------------------------------------------
# Main
# ------------------------------------------------------------

def main():
    st.set_page_config(page_title=APP_TITLE, layout="wide")
    st.title(APP_TITLE)

    # 사이드바 메뉴
    menu = st.sidebar.radio(
        "메뉴",
        ["대시보드", "교과 관리", "수업 관리", "학생 관리", "수업 일지", "출결 관리", "일자별 전체 조회"],
    )

    # 전역 빠른 입력 다이얼로그 오픈 핸들러
    if st.session_state.get("__open_progress__"):
        st.session_state["__open_progress__"] = False
        # 필요한 클래스 정보가 없으므로 직접 view에서 열도록 유지
    if st.session_state.get("__open_attendance__"):
        st.session_state["__open_attendance__"] = False

    try:
        if menu == "대시보드":
            dashboard_view()
        elif menu == "교과 관리":
            course_view()
        elif menu == "수업 관리":
            class_view()
        elif menu == "학생 관리":
            student_view()
        elif menu == "수업 일지":
            progress_view()
        elif menu == "출결 관리":
            attendance_view()
        elif menu == "일자별 전체 조회":
            daily_overview_view()
    except Exception as e:
        st.error(f"처리 중 오류가 발생했습니다: {e}")
        st.exception(e)


if __name__ == "__main__":
    main()
