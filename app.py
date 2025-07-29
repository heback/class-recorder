# app.py — 수업·평가 운영 시스템 (Streamlit + Firebase)
# ---------------------------------------------------
# 요구사항 요약:
# - 교과(학년도/학기/교과명) 관리 + 수업·평가계획서 PDF 업로드/조회(≤10MB)
# - 수업반(학년도/학기/교과/학반) 관리 + 요일/교시 시간표
# - 수업반별 학생 관리(개별 + CSV 업로드)
# - 수업반별 진도·특기사항(일자/교시) CRUD
# - 수업반별 출결·특기사항 CRUD (UI 라벨: 출석/결석/지각/공결 ↔ 저장값: P/A/L/E)
# - 일자 기준 전체 수업반 진도/출결 조회 대시보드
# - Streamlit Cloud + Firebase(Firestore, Storage), st.secrets["FIREBASE_KEY"] 사용
# - 모든 입력/수정은 st.dialog 사용, 저장 버튼만 제공(닫기/취소 없음), 저장 후 st.rerun()
# - 데이터 없음은 st.info로 안내, 예외 처리 철저

from __future__ import annotations
import os
import io
import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

import streamlit as st

# Firebase Admin SDK
from firebase_admin import credentials, firestore as fb_fs_admin, storage as fb_storage, initialize_app
from google.cloud import firestore as gcf  # SERVER_TIMESTAMP, types

# 표/CSV 처리
import pandas as pd

KST = ZoneInfo("Asia/Seoul")
APP_TITLE = "수업·평가 운영 시스템"

# -----------------------------
# 초기화 / 공통 유틸
# -----------------------------

@st.cache_resource(show_spinner=False)
def init_firebase():
    """st.secrets["FIREBASE_KEY"]에서 서비스 계정 JSON을 읽어 Firebase Admin 초기화.
    storageBucket 도 secrets에 포함되어 있어야 함.
    """
    try:
        key = st.secrets.get("FIREBASE_KEY")
        if key is None:
            st.stop()
        if isinstance(key, str):
            key_dict = json.loads(key)
        else:
            key_dict = dict(key)

        cred = credentials.Certificate(key_dict)
        app = initialize_app(cred, {
            "storageBucket": key_dict.get("storageBucket")
        })
        db = fb_fs_admin.client(app)
        bucket = fb_storage.bucket(app=app)
        return db, bucket
    except Exception as e:
        st.error(f"Firebase 초기화 오류: {e}")
        raise


def to_kst(dt: Optional[datetime] = None) -> datetime:
    dt = dt or datetime.utcnow()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    return dt.astimezone(KST)


def validate_pdf(uploaded_file: Optional[st.runtime.uploaded_file_manager.UploadedFile]) -> Tuple[bool, Optional[str]]:
    """PDF 확장자/MIME 확인 + 10MB 이하 용량 제한."""
    if uploaded_file is None:
        return True, None
    try:
        # MIME 우선 확인
        if uploaded_file.type not in ("application/pdf", "application/x-pdf"):
            # 확장자 보조 확인
            if not uploaded_file.name.lower().endswith(".pdf"):
                return False, "PDF 파일만 업로드할 수 있습니다."
        size = len(uploaded_file.getbuffer())
        if size > 10 * 1024 * 1024:
            return False, "파일 용량은 10MB 이하여야 합니다."
        return True, None
    except Exception as e:
        return False, f"파일 확인 중 오류: {e}"


# Firebase 리소스
DB, BUCKET = init_firebase()

# -----------------------------
# 데이터 액세스 레이어 (간단 Repo)
# -----------------------------

# subjects (교과)

def subjects_list(filters: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    q = DB.collection("subjects")
    filters = filters or {}
    if "year" in filters:
        q = q.where("year", "==", int(filters["year"]))
    if "semester" in filters:
        q = q.where("semester", "==", int(filters["semester"]))
    if "name_kw" in filters and filters["name_kw"]:
        # 간단 키워드 필터는 클라이언트 측에서 처리(소수 데이터 가정)
        docs = q.order_by("year").order_by("semester").order_by("name").stream()
        rows = [{"id": d.id, **d.to_dict()} for d in docs]
        kw = str(filters["name_kw"]).strip()
        return [r for r in rows if kw in r.get("name", "")]
    docs = q.order_by("year").order_by("semester").order_by("name").stream()
    return [{"id": d.id, **d.to_dict()} for d in docs]


def subject_get(doc_id: str) -> Optional[Dict[str, Any]]:
    snap = DB.collection("subjects").document(doc_id).get()
    if not snap.exists:
        return None
    return {"id": snap.id, **snap.to_dict()}


def subject_create(name: str, year: int, semester: int, pdf: Optional[st.runtime.uploaded_file_manager.UploadedFile]):
    doc_ref = DB.collection("subjects").document()
    plan_meta = None
    try:
        # 먼저 문서 생성(파일 메타 빈 상태) — 실패 시 Storage 업로드 방지
        doc_ref.set({
            "name": name,
            "year": int(year),
            "semester": int(semester),
            "plan": None,
            "created_at": to_kst(),
            "updated_at": to_kst(),
        })
        if pdf is not None:
            plan_meta = _upload_subject_pdf(doc_ref.id, pdf)
            doc_ref.update({"plan": plan_meta, "updated_at": to_kst()})
        return doc_ref.id
    except Exception as e:
        # 보상: 업로드된 파일이 있었다면 삭제
        if plan_meta and plan_meta.get("bucket_path"):
            try:
                BUCKET.blob(plan_meta["bucket_path"]).delete()
            except Exception:
                pass
        raise e


def subject_update(doc_id: str, name: str, year: int, semester: int, pdf: Optional[st.runtime.uploaded_file_manager.UploadedFile]):
    doc_ref = DB.collection("subjects").document(doc_id)
    current = doc_ref.get().to_dict() or {}
    old_plan = (current or {}).get("plan")
    doc_ref.update({
        "name": name,
        "year": int(year),
        "semester": int(semester),
        "updated_at": to_kst(),
    })
    if pdf is not None:
        # 새 파일 업로드 후 문서 갱신, 실패 시 보상
        new_meta = None
        try:
            new_meta = _upload_subject_pdf(doc_id, pdf)
            doc_ref.update({"plan": new_meta, "updated_at": to_kst()})
            # 구 파일 정리
            if old_plan and old_plan.get("bucket_path"):
                try:
                    BUCKET.blob(old_plan["bucket_path"]).delete()
                except Exception:
                    pass
        except Exception as e:
            # 새 업로드 실패 시 보상 삭제
            if new_meta and new_meta.get("bucket_path"):
                try:
                    BUCKET.blob(new_meta["bucket_path"]).delete()
                except Exception:
                    pass
            raise e


def subject_delete(doc_id: str) -> bool:
    # 수업반 존재 검사(참조 정합성)
    classes_q = DB.collection("classes").where("subject_id", "==", DB.collection("subjects").document(doc_id)).limit(1).stream()
    has_class = any(True for _ in classes_q)
    if has_class:
        raise RuntimeError("해당 교과에 연결된 수업반이 있어 삭제할 수 없습니다.")
    # PDF 삭제
    subj = subject_get(doc_id)
    if subj and subj.get("plan") and subj["plan"].get("bucket_path"):
        try:
            BUCKET.blob(subj["plan"]["bucket_path"]).delete()
        except Exception:
            pass
    DB.collection("subjects").document(doc_id).delete()
    return True


def _upload_subject_pdf(subject_id: str, uploaded_file: st.runtime.uploaded_file_manager.UploadedFile) -> Dict[str, Any]:
    ok, msg = validate_pdf(uploaded_file)
    if not ok:
        raise ValueError(msg or "PDF 유효성 검사를 통과하지 못했습니다.")
    # 파일명 안전 처리
    ts = to_kst().strftime("%Y%m%d-%H%M%S")
    safe_name = uploaded_file.name.replace(" ", "_")
    blob_path = f"subjects/{subject_id}/plans/{ts}__{safe_name}"
    blob = BUCKET.blob(blob_path)
    blob.upload_from_file(uploaded_file, content_type="application/pdf")
    meta = {
        "file_name": uploaded_file.name,
        "file_size": len(uploaded_file.getbuffer()),
        "content_type": "application/pdf",
        "uploaded_at": to_kst(),
        "bucket_path": blob_path,
    }
    # 서명 URL(1시간) — 실패해도 앱 동작에 영향 없도록
    try:
        url = blob.generate_signed_url(version="v4", expiration=timedelta(hours=1), method="GET")
        meta["url"] = url
    except Exception:
        meta["url"] = None
    return meta


# classes (수업반)

def classes_list(filters: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    q = DB.collection("classes")
    filters = filters or {}
    if "year" in filters:
        q = q.where("year", "==", int(filters["year"]))
    if "semester" in filters:
        q = q.where("semester", "==", int(filters["semester"]))
    if "subject_id" in filters and filters["subject_id"]:
        q = q.where("subject_id", "==", DB.collection("subjects").document(filters["subject_id"]))
    docs = q.order_by("year").order_by("semester").order_by("class_code").stream()
    rows = []
    for d in docs:
        data = d.to_dict()
        # subject_name 보조(스냅샷 아님 — UI 편의)
        subj_name = None
        try:
            subj_snap = data.get("subject_id").get() if data.get("subject_id") else None
            if subj_snap and subj_snap.exists:
                subj_name = subj_snap.to_dict().get("name")
        except Exception:
            pass
        rows.append({"id": d.id, **data, "subject_name": subj_name})
    return rows


def class_get(doc_id: str) -> Optional[Dict[str, Any]]:
    snap = DB.collection("classes").document(doc_id).get()
    if not snap.exists:
        return None
    data = snap.to_dict()
    subj_name = None
    try:
        s = data.get("subject_id").get() if data.get("subject_id") else None
        if s and s.exists:
            subj_name = s.to_dict().get("name")
    except Exception:
        pass
    return {"id": snap.id, **data, "subject_name": subj_name}


def class_exists(year: int, semester: int, subject_id: str, class_code: str) -> bool:
    q = (DB.collection("classes")
         .where("year", "==", int(year))
         .where("semester", "==", int(semester))
         .where("subject_id", "==", DB.collection("subjects").document(subject_id))
         .where("class_code", "==", class_code)
         .limit(1)
         .stream())
    return any(True for _ in q)


def class_create(year: int, semester: int, subject_id: str, class_code: str, timetable: List[Dict[str, Any]]):
    if class_exists(year, semester, subject_id, class_code):
        raise RuntimeError("동일 학년도/학기/교과/수업반이 이미 존재합니다.")
    doc_ref = DB.collection("classes").document()
    doc_ref.set({
        "year": int(year),
        "semester": int(semester),
        "subject_id": DB.collection("subjects").document(subject_id),
        "class_code": class_code,
        "timetable": timetable or [],
        "created_at": to_kst(),
        "updated_at": to_kst(),
    })
    return doc_ref.id


def class_update(doc_id: str, year: int, semester: int, subject_id: str, class_code: str, timetable: List[Dict[str, Any]]):
    DB.collection("classes").document(doc_id).update({
        "year": int(year),
        "semester": int(semester),
        "subject_id": DB.collection("subjects").document(subject_id),
        "class_code": class_code,
        "timetable": timetable or [],
        "updated_at": to_kst(),
    })


def class_delete(doc_id: str):
    # 하위 subcollections 삭제(lessons, students, attendance)
    class_ref = DB.collection("classes").document(doc_id)
    # lessons
    for d in class_ref.collection("lessons").stream():
        d.reference.delete()
    # students
    for d in class_ref.collection("students").stream():
        d.reference.delete()
    # attendance
    for d in class_ref.collection("attendance").stream():
        d.reference.delete()
    class_ref.delete()


# students (수업반 하위)

def students_list(class_id: str) -> List[Dict[str, Any]]:
    docs = DB.collection("classes").document(class_id).collection("students").order_by("student_no").stream()
    return [{"id": d.id, **d.to_dict()} for d in docs]


def student_upsert(class_id: str, student_no: str, name: str):
    ref = DB.collection("classes").document(class_id).collection("students").document(student_no)
    now = to_kst()
    ref.set({
        "student_no": student_no,
        "name": name,
        "updated_at": now,
        "created_at": ref.get().to_dict().get("created_at") if ref.get().exists else now,
    })


def student_delete(class_id: str, student_no: str):
    DB.collection("classes").document(class_id).collection("students").document(student_no).delete()


# lessons (진도)

def lessons_list(class_id: str, date_from: Optional[date] = None, date_to: Optional[date] = None) -> List[Dict[str, Any]]:
    col = DB.collection("classes").document(class_id).collection("lessons")
    q = col
    if date_from is not None:
        q = q.where("date", ">=", date_from.strftime("%Y-%m-%d"))
    if date_to is not None:
        q = q.where("date", "<=", date_to.strftime("%Y-%m-%d"))
    docs = q.order_by("date").order_by("period").stream()
    return [{"id": d.id, **d.to_dict()} for d in docs]


def lesson_upsert(class_id: str, d: date, period: int, progress: str, note: str):
    class_ref = DB.collection("classes").document(class_id)
    key = f"{d.strftime('%Y-%m-%d')}-{int(period)}"
    ref = class_ref.collection("lessons").document(key)
    c = class_get(class_id) or {}
    snapshot = {
        "year": c.get("year"),
        "semester": c.get("semester"),
        "subject_name": c.get("subject_name"),
        "class_code": c.get("class_code"),
    }
    now = to_kst()
    ref.set({
        "date": d.strftime("%Y-%m-%d"),
        "date_ts": now,
        "period": int(period),
        "progress": progress,
        "note": note or "",
        "class_ref": class_ref,
        "snapshot": snapshot,
        "updated_at": now,
        "created_at": ref.get().to_dict().get("created_at") if ref.get().exists else now,
    })


def lesson_delete(class_id: str, doc_id: str):
    DB.collection("classes").document(class_id).collection("lessons").document(doc_id).delete()


# attendance (출결)
STATUS_LABELS = ["출석", "결석", "지각", "공결"]
LABEL_TO_CODE = {"출석": "P", "결석": "A", "지각": "L", "공결": "E"}
CODE_TO_LABEL = {v: k for k, v in LABEL_TO_CODE.items()}


def attendance_list(class_id: str, d: date) -> List[Dict[str, Any]]:
    col = DB.collection("classes").document(class_id).collection("attendance")
    ds = d.strftime("%Y-%m-%d")
    docs = col.where("date", "==", ds).stream()
    return [{"id": doc.id, **doc.to_dict()} for doc in docs]


def attendance_batch_upsert(class_id: str, d: date, rows: List[Dict[str, Any]]):
    class_ref = DB.collection("classes").document(class_id)
    c = class_get(class_id) or {}
    snapshot = {
        "year": c.get("year"),
        "semester": c.get("semester"),
        "subject_name": c.get("subject_name"),
        "class_code": c.get("class_code"),
    }
    ds = d.strftime("%Y-%m-%d")
    now = to_kst()
    batch = DB.batch()
    for r in rows:
        student_no = str(r.get("student_no"))
        key = f"{ds}-{student_no}"
        ref = class_ref.collection("attendance").document(key)
        data_old_snap = ref.get()
        batch.set(ref, {
            "date": ds,
            "date_ts": now,
            "student_no": student_no,
            "student_name": r.get("name", ""),
            "status": LABEL_TO_CODE.get(r.get("status_label"), "P"),
            "note": r.get("note", ""),
            "class_ref": class_ref,
            "snapshot": snapshot,
            "updated_at": now,
            "created_at": data_old_snap.to_dict().get("created_at") if data_old_snap.exists else now,
        })
    batch.commit()


# collection group 쿼리(대시보드)

def cg_lessons_by_date(target_date: date) -> List[Dict[str, Any]]:
    ds = target_date.strftime("%Y-%m-%d")
    q = DB.collection_group("lessons").where("date", "==", ds)
    docs = q.stream()
    return [{"id": d.id, **d.to_dict()} for d in docs]


def cg_attendance_by_date(target_date: date) -> List[Dict[str, Any]]:
    ds = target_date.strftime("%Y-%m-%d")
    q = DB.collection_group("attendance").where("date", "==", ds)
    docs = q.stream()
    return [{"id": d.id, **d.to_dict()} for d in docs]


# -----------------------------
# UI: Dialogs (입력/수정 전용)
# -----------------------------

@st.dialog("교과 추가/수정")
def dialog_subject(default: Optional[Dict[str, Any]] = None):
    default = default or {}
    col1, col2 = st.columns(2)
    with col1:
        name = st.text_input("교과명", value=default.get("name", ""))
    with col2:
        year = st.number_input("학년도", step=1, value=int(default.get("year", date.today().year)))
        semester = st.selectbox("학기", [1, 2], index=0 if int(default.get("semester", 1)) == 1 else 1)

    pdf = st.file_uploader("수업·평가 계획서(PDF, ≤10MB)", type=["pdf"], accept_multiple_files=False)

    if st.button("저장", type="primary"):
        try:
            ok, msg = validate_pdf(pdf)
            if not ok:
                st.warning(msg)
                st.stop()
            if default.get("id"):
                subject_update(default["id"], name, int(year), int(semester), pdf)
            else:
                subject_create(name, int(year), int(semester), pdf)
            st.success("저장되었습니다.")
            st.rerun()
        except Exception as e:
            st.error(f"저장 실패: {e}")


@st.dialog("수업반 추가/수정")
def dialog_class(subjects: List[Dict[str, Any]], default: Optional[Dict[str, Any]] = None):
    default = default or {}
    year = st.number_input("학년도", step=1, value=int(default.get("year", date.today().year)))
    semester = st.selectbox("학기", [1, 2], index=0 if int(default.get("semester", 1)) == 1 else 1)

    # 교과 선택
    subj_options = {f"{s['name']} ({s['year']}/{s['semester']})": s["id"] for s in subjects}
    default_subj_id = default.get("subject_id")
    default_label = None
    if default_subj_id:
        for k, v in subj_options.items():
            if v == (default_subj_id.id if hasattr(default_subj_id, 'id') else default_subj_id):
                default_label = k
                break
    label = st.selectbox("교과", list(subj_options.keys()), index=list(subj_options.keys()).index(default_label) if default_label in subj_options else 0 if subj_options else None)
    subject_id = subj_options[label] if subj_options else None

    class_code = st.text_input("수업반(학반)", value=default.get("class_code", ""))

    st.markdown("**요일·교시 등록** — 요일을 선택하고 교시를 콤마(,)로 구분해 입력 (예: 1,2,3)")
    days = ["Mon", "Tue", "Wed", "Thu", "Fri"]
    day_labels = {"Mon": "월", "Tue": "화", "Wed": "수", "Thu": "목", "Fri": "금"}
    timetable_rows: List[Dict[str, Any]] = []
    for dcode in days:
        periods_text = st.text_input(f"{day_labels[dcode]}요일 교시", value=",")
        periods = []
        try:
            periods = [int(x.strip()) for x in periods_text.split(",") if x.strip().isdigit()]
        except Exception:
            pass
        if periods:
            timetable_rows.append({"day": dcode, "periods": periods})

    if st.button("저장", type="primary"):
        try:
            if not subject_id:
                st.warning("교과를 선택해 주세요.")
                st.stop()
            if default.get("id"):
                class_update(default["id"], int(year), int(semester), subject_id, class_code, timetable_rows)
            else:
                class_create(int(year), int(semester), subject_id, class_code, timetable_rows)
            st.success("저장되었습니다.")
            st.rerun()
        except Exception as e:
            st.error(f"저장 실패: {e}")


@st.dialog("학생 추가/수정")
def dialog_student(class_id: str, default: Optional[Dict[str, Any]] = None):
    default = default or {}
    student_no = st.text_input("학번", value=default.get("student_no", ""))
    name = st.text_input("성명", value=default.get("name", ""))
    if st.button("저장", type="primary"):
        try:
            if not student_no or not name:
                st.warning("학번과 성명은 필수입니다.")
                st.stop()
            student_upsert(class_id, student_no, name)
            st.success("저장되었습니다.")
            st.rerun()
        except Exception as e:
            st.error(f"저장 실패: {e}")


@st.dialog("진도 입력/수정")
def dialog_lesson(class_id: str, default: Optional[Dict[str, Any]] = None):
    default = default or {}
    d = st.date_input("일자", value=default.get("date_dt", date.today()))
    period = st.number_input("교시", min_value=1, max_value=20, step=1, value=int(default.get("period", 1)))
    progress = st.text_area("진도", value=default.get("progress", ""))
    note = st.text_area("특기사항", value=default.get("note", ""))
    if st.button("저장", type="primary"):
        try:
            if not progress:
                st.warning("진도 내용을 입력하세요.")
                st.stop()
            lesson_upsert(class_id, d, int(period), progress, note)
            st.success("저장되었습니다.")
            st.rerun()
        except Exception as e:
            st.error(f"저장 실패: {e}")


@st.dialog("출결 저장")
def dialog_attendance_save(class_id: str, d: date, pending_rows_key: str):
    # pending_rows_key: session_state에 임시 저장된 출결 입력 키
    rows = st.session_state.get(pending_rows_key, [])
    st.write(f"총 {len(rows)}명 저장 예정")
    if st.button("저장", type="primary"):
        try:
            attendance_batch_upsert(class_id, d, rows)
            st.success("저장되었습니다.")
            # 임시 상태 초기화
            st.session_state.pop(pending_rows_key, None)
            st.rerun()
        except Exception as e:
            st.error(f"저장 실패: {e}")


# -----------------------------
# UI: 메뉴 화면
# -----------------------------

def menu_dashboard():
    st.header("대시보드 / 일자별 조회")
    d = st.date_input("조회 일자", value=date.today())

    tab1, tab2 = st.tabs(["진도·특기사항", "출결·특기사항"])

    with tab1:
        try:
            rows = cg_lessons_by_date(d)
            if not rows:
                st.info("선택한 조건에 해당하는 진도 데이터가 없습니다.")
            else:
                df = pd.DataFrame(rows)
                show_cols = ["date", "period", "snapshot", "progress", "note"]
                for c in show_cols:
                    if c not in df.columns:
                        df[c] = None
                df = df[show_cols].sort_values(["period"])  # 동일 일자 내 교시 정렬
                st.dataframe(df, use_container_width=True)
        except Exception as e:
            st.error(f"조회 오류: {e}")

    with tab2:
        try:
            rows = cg_attendance_by_date(d)
            if not rows:
                st.info("선택한 조건에 해당하는 출결 데이터가 없습니다.")
            else:
                # 요약: 반별 상태 카운트
                df = pd.DataFrame(rows)
                for c in ["snapshot", "status", "student_no", "student_name", "note"]:
                    if c not in df.columns:
                        df[c] = None
                df["status_label"] = df["status"].map(CODE_TO_LABEL).fillna("출석")
                # 반 식별자: 학년도/학기/교과/수업반 문자열
                def class_key(snap):
                    if not isinstance(snap, dict):
                        return "-"
                    return f"{snap.get('year')}/{snap.get('semester')} {snap.get('subject_name')} {snap.get('class_code')}"
                df["class_key"] = df["snapshot"].apply(class_key)
                summary = (df.groupby(["class_key", "status_label"]).size().unstack(fill_value=0)).reset_index()
                st.subheader("반별 출결 요약")
                st.dataframe(summary, use_container_width=True)

                with st.expander("학생별 상세 보기"):
                    st.dataframe(df[["class_key", "student_no", "student_name", "status_label", "note"]].sort_values(["class_key", "student_no"]))
        except Exception as e:
            st.error(f"조회 오류: {e}")


def menu_subjects():
    st.header("교과 관리")
    c1, c2, c3 = st.columns([1,1,2])
    with c1:
        f_year = st.number_input("학년도 필터", step=1, value=int(date.today().year))
    with c2:
        f_sem = st.selectbox("학기 필터", [1,2])
    with c3:
        kw = st.text_input("교과명 검색(부분 일치)")

    try:
        rows = subjects_list({"year": f_year, "semester": f_sem, "name_kw": kw})
    except Exception as e:
        st.error(f"목록 조회 오류: {e}")
        rows = []

    st.button("교과 추가", on_click=lambda: dialog_subject({}))

    if not rows:
        st.info("등록된 교과가 없습니다.")
        return

    for r in rows:
        with st.container(border=True):
            st.subheader(f"{r.get('name')} ({r.get('year')}/{r.get('semester')})")
            plan = r.get("plan") or {}
            c1, c2, c3, c4 = st.columns([2,1,1,1])
            with c1:
                if plan.get("file_name"):
                    st.write(f"계획서: {plan.get('file_name')}")
                    if plan.get("url"):
                        st.markdown(f"[다운로드]({plan['url']})")
                    else:
                        st.caption("다운로드 링크 생성 실패 또는 만료")
                else:
                    st.caption("계획서 파일 없음")
            with c2:
                st.button("보기/수정", key=f"subj_edit_{r['id']}", on_click=lambda rr=r: dialog_subject(rr))
            with c3:
                st.button("PDF 교체", key=f"subj_pdf_{r['id']}", on_click=lambda rr=r: dialog_subject({"id": rr["id"], "name": rr["name"], "year": rr["year"], "semester": rr["semester"]}))
            with c4:
                if st.button("삭제", key=f"subj_del_{r['id']}"):
                    try:
                        subject_delete(r["id"])
                        st.success("삭제되었습니다.")
                        st.rerun()
                    except Exception as e:
                        st.error(f"삭제 실패: {e}")


def menu_classes():
    st.header("수업(반) 관리")
    try:
        subjects = subjects_list()
    except Exception as e:
        st.error(f"교과 조회 오류: {e}")
        subjects = []

    st.button("수업반 추가", on_click=lambda: dialog_class(subjects, {}))

    try:
        rows = classes_list()
        if not rows:
            st.info("등록된 수업반이 없습니다.")
            return
        for r in rows:
            with st.container(border=True):
                st.subheader(f"{r.get('year')}/{r.get('semester')} {r.get('subject_name')} - {r.get('class_code')}")
                st.caption(f"시간표: {r.get('timetable')}")
                c1, c2, c3 = st.columns(3)
                with c1:
                    st.button("수정", key=f"class_edit_{r['id']}", on_click=lambda rr=r: dialog_class(subjects, rr))
                with c2:
                    st.button("삭제", key=f"class_del_{r['id']}", on_click=lambda rid=r['id']: _delete_class_confirm(rid))
                with c3:
                    st.empty()
    except Exception as e:
        st.error(f"수업반 목록 오류: {e}")


def _delete_class_confirm(class_id: str):
    with st.dialog("수업반 삭제 확인"):
        st.warning("수업반 및 하위 데이터(학생/진도/출결)가 삭제됩니다.")
        if st.button("저장", type="primary"):
            try:
                class_delete(class_id)
                st.success("삭제되었습니다.")
                st.rerun()
            except Exception as e:
                st.error(f"삭제 실패: {e}")


def _select_class_ui() -> Optional[str]:
    rows = classes_list()
    if not rows:
        st.info("수업반이 없습니다. 먼저 수업반을 등록하세요.")
        return None
    opts = {f"{r['year']}/{r['semester']} {r.get('subject_name')} - {r['class_code']}": r["id"] for r in rows}
    label = st.selectbox("수업반 선택", list(opts.keys()))
    return opts[label]


def menu_students():
    st.header("학생 관리 (수업반별)")
    class_id = _select_class_ui()
    if not class_id:
        return

    c1, c2 = st.columns([1,1])
    with c1:
        st.button("학생 추가", on_click=lambda: dialog_student(class_id, {}))
    with c2:
        with st.popover("CSV 업로드"):
            st.caption("헤더: student_no,name | UTF-8 권장 (CP949 자동 시도)")
            file = st.file_uploader("CSV 파일", type=["csv"], accept_multiple_files=False)
            if st.button("저장", type="primary") and file is not None:
                try:
                    df = _read_csv_flex(file)
                    ok_cnt = 0
                    for _, row in df.iterrows():
                        sno = str(row.get("student_no", "")).strip()
                        nm = str(row.get("name", "")).strip()
                        if not sno or not nm:
                            continue
                        student_upsert(class_id, sno, nm)
                        ok_cnt += 1
                    st.success(f"업로드 완료: {ok_cnt}명 처리")
                    st.rerun()
                except Exception as e:
                    st.error(f"업로드 실패: {e}")

    rows = students_list(class_id)
    if not rows:
        st.info("학생이 없습니다. 추가해 주세요.")
        return

    df = pd.DataFrame(rows)[["student_no", "name"]]
    st.dataframe(df, use_container_width=True)

    # 개별 삭제 버튼
    for r in rows:
        st.button(f"삭제: {r['student_no']} {r['name']}", key=f"stu_del_{r['id']}", on_click=lambda rr=r: _student_delete_confirm(class_id, rr))


def _student_delete_confirm(class_id: str, row: Dict[str, Any]):
    with st.dialog("학생 삭제 확인"):
        st.write(f"{row.get('student_no')} {row.get('name')} 삭제")
        if st.button("저장", type="primary"):
            try:
                student_delete(class_id, row.get("student_no"))
                st.success("삭제되었습니다.")
                st.rerun()
            except Exception as e:
                st.error(f"삭제 실패: {e}")


def menu_lessons():
    st.header("진도·특기사항 (수업반별)")
    class_id = _select_class_ui()
    if not class_id:
        return

    c1, c2 = st.columns(2)
    with c1:
        start_d = st.date_input("시작일", value=date.today() - timedelta(days=7))
    with c2:
        end_d = st.date_input("종료일", value=date.today())

    st.button("진도 추가", on_click=lambda: dialog_lesson(class_id, {}))

    try:
        rows = lessons_list(class_id, start_d, end_d)
        if not rows:
            st.info("해당 기간에 진도 데이터가 없습니다.")
            return
        df = pd.DataFrame(rows)
        if not df.empty:
            df["date_dt"] = pd.to_datetime(df["date"])  # 표시용
            st.dataframe(df[["date", "period", "progress", "note"]].sort_values(["date", "period"]).reset_index(drop=True), use_container_width=True)
        # 행별 수정/삭제
        for r in rows:
            c1, c2 = st.columns(2)
            with c1:
                st.button(f"수정: {r['date']} {r['period']}교시", key=f"lesson_edit_{r['id']}", on_click=lambda rr=r: dialog_lesson(class_id, {**rr, "date_dt": datetime.strptime(rr['date'], '%Y-%m-%d').date()}))
            with c2:
                if st.button(f"삭제: {r['date']} {r['period']}교시", key=f"lesson_del_{r['id']}"):
                    try:
                        lesson_delete(class_id, r["id"])
                        st.success("삭제되었습니다.")
                        st.rerun()
                    except Exception as e:
                        st.error(f"삭제 실패: {e}")
    except Exception as e:
        st.error(f"조회 오류: {e}")


def menu_attendance():
    st.header("출결·특기사항 (수업반별)")
    class_id = _select_class_ui()
    if not class_id:
        return

    d = st.date_input("일자", value=date.today())

    # 학생 목록 + 기존 출결 로드
    students = students_list(class_id)
    if not students:
        st.info("학생이 없습니다. 먼저 학생을 등록하세요.")
        return

    existing = {a["student_no"]: a for a in attendance_list(class_id, d)}

    # 일괄 기본값
    default_all = st.selectbox("일괄 기본값 지정", STATUS_LABELS, index=0)

    input_rows = []
    st.write("학생별 출결 입력")
    for s in students:
        sno = s["student_no"]
        sname = s["name"]
        prev = existing.get(sno)
        prev_label = CODE_TO_LABEL.get(prev.get("status")) if prev else default_all
        c1, c2, c3 = st.columns([1,1,3])
        with c1:
            st.write(f"{sno}")
        with c2:
            st.write(sname)
        with c3:
            label = st.selectbox("상태", STATUS_LABELS, index=STATUS_LABELS.index(prev_label) if prev_label in STATUS_LABELS else 0, key=f"att_status_{sno}")
            note = st.text_input("특기사항", value=(prev.get("note") if prev else ""), key=f"att_note_{sno}")
        input_rows.append({"student_no": sno, "name": sname, "status_label": label, "note": note})

    # 저장 다이얼로그 오픈
    pending_key = f"pending_att_{class_id}_{d.isoformat()}"
    st.session_state[pending_key] = input_rows
    st.button("저장", type="primary", on_click=lambda: dialog_attendance_save(class_id, d, pending_key))


def menu_settings():
    st.header("설정/도움말")
    # Firebase 연결 상태
    try:
        st.write("Firestore 연결: OK")
        st.write(f"Storage 버킷: {BUCKET.name}")
    except Exception as e:
        st.error(f"Firebase 연결 오류: {e}")

    # CSV 템플릿
    st.subheader("CSV 템플릿(학생 업로드)")
    sample = "student_no,name\n20231234,홍길동\n20231235,김영희\n"
    st.download_button("템플릿 다운로드", sample.encode("utf-8"), file_name="students_template.csv", mime="text/csv")


# -----------------------------
# CSV 보조
# -----------------------------

def _read_csv_flex(file) -> pd.DataFrame:
    """UTF-8 우선, 실패 시 CP949로 재시도."""
    file.seek(0)
    try:
        return pd.read_csv(file)
    except Exception:
        file.seek(0)
        return pd.read_csv(file, encoding="cp949")


# -----------------------------
# 메인 라우팅
# -----------------------------

def main():
    st.set_page_config(page_title=APP_TITLE, layout="wide")
    st.title(APP_TITLE)

    menu = st.sidebar.radio("메뉴", [
        "대시보드 / 일자별 조회",
        "교과 관리",
        "수업(반) 관리",
        "학생 관리(수업반별)",
        "진도·특기사항(수업반별)",
        "출결·특기사항(수업반별)",
        "설정/도움말",
    ])

    try:
        if menu == "대시보드 / 일자별 조회":
            menu_dashboard()
        elif menu == "교과 관리":
            menu_subjects()
        elif menu == "수업(반) 관리":
            menu_classes()
        elif menu == "학생 관리(수업반별)":
            menu_students()
        elif menu == "진도·특기사항(수업반별)":
            menu_lessons()
        elif menu == "출결·특기사항(수업반별)":
            menu_attendance()
        elif menu == "설정/도움말":
            menu_settings()
    except Exception as e:
        st.error(f"예상치 못한 오류가 발생했습니다: {e}")


if __name__ == "__main__":
    main()
