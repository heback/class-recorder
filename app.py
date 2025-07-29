# -*- coding: utf-8 -*-
"""
수업·평가 관리 앱 (Streamlit + Firebase Firestore/Storage + Google Sheets Export)
- 하나의 파일(app.py)에 모든 구현
- 인증: Streamlit Secrets -> FIREBASE_KEY(dict) 사용, storageBucket 포함
- 파일 업로드: PDF만, 최대 10MB
- 입력/수정: st.dialog 사용, 저장 후 st.rerun()
- 빈 데이터: st.info 안내
- 메뉴: 사이드바 selectbox
- Google Sheets 내보내기: 선택한 Firestore 컬렉션을 동일한 스프레드시트 내 시트명=컬렉션명으로 생성

필요 패키지 (requirements.txt):
streamlit
google-cloud-firestore
google-cloud-storage
google-auth
pandas
gspread
"""
from __future__ import annotations
import json
import io
import time
from typing import List, Dict, Any, Optional, Tuple

import streamlit as st
import pandas as pd
from datetime import datetime, timezone

from google.oauth2 import service_account
from google.cloud import firestore
from google.cloud.firestore import Client as FirestoreClient
from google.cloud.firestore_v1 import SERVER_TIMESTAMP
from google.cloud import storage

import gspread

# ------------------------------
# 초기 설정
# ------------------------------
st.set_page_config(page_title="수업·평가 관리", layout="wide")

APP_TZ = "Asia/Seoul"  # 표기 목적(서버 로컬 시간 사용)
MAX_PDF_BYTES = 10 * 1024 * 1024  # 10MB

COL_SUBJECTS = "subjects"
COL_CLASSES = "class_sections"
COL_STUDENTS = "class_students"
COL_LESSON_LOGS = "lesson_logs"
COL_ATTENDANCE = "attendance"
COL_EXPORTS = "exports"

ATTENDANCE_STATES = ["present", "absent", "late", "excused"]
WEEKDAYS = {1:"월",2:"화",3:"수",4:"목",5:"금",6:"토",7:"일"}

# ------------------------------
# Firebase 초기화
# ------------------------------
@st.cache_resource(show_spinner=False)
def init_clients() -> Tuple[FirestoreClient, storage.Bucket, Dict[str, Any]]:
    raw = st.secrets.get("FIREBASE_KEY", None)
    if raw is None:
        st.stop()
    if isinstance(raw, str):
        key = json.loads(raw)
    else:
        # SecretsToml contains a mapping
        key = dict(raw)
    required = ["project_id", "client_email", "private_key", "storageBucket"]
    for k in required:
        if k not in key:
            st.error(f"FIREBASE_KEY에 '{k}'가 없습니다. Streamlit Secrets를 확인하세요.")
            st.stop()
    creds = service_account.Credentials.from_service_account_info(key)
    fs = firestore.Client(project=key["project_id"], credentials=creds)
    storage_client = storage.Client(project=key["project_id"], credentials=creds)
    bucket = storage_client.bucket(key["storageBucket"])
    return fs, bucket, key

fs, bucket, FIREBASE_INFO = init_clients()

# ------------------------------
# 유틸리티
# ------------------------------
def now_ts_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")

def rerun():
    st.rerun()

# 파일 검증: PDF & 사이즈
def validate_pdf(uploaded: Optional[st.runtime.uploaded_file_manager.UploadedFile]) -> Tuple[bool, Optional[str]]:
    if uploaded is None:
        return False, "파일이 선택되지 않았습니다."
    name = (uploaded.name or "").lower()
    if not name.endswith(".pdf"):
        return False, "PDF 파일만 업로드 가능합니다. (.pdf)"
    try:
        size = uploaded.size if hasattr(uploaded, "size") else len(uploaded.getbuffer())
    except Exception:
        # fallback
        size = len(uploaded.read())
        uploaded.seek(0)
    if size > MAX_PDF_BYTES:
        return False, "파일 용량은 10MB 이하여야 합니다."
    return True, None

# Storage 업로드
def upload_pdf_to_storage(subject_id: str, uploaded: st.runtime.uploaded_file_manager.UploadedFile) -> Tuple[str, str, int]:
    path = f"subjects/{subject_id}/syllabus.pdf"
    blob = bucket.blob(path)
    uploaded.seek(0)
    blob.upload_from_file(uploaded, content_type="application/pdf")
    blob.cache_control = "public, max-age=3600"
    blob.patch()
    # 공개 URL은 퍼블릭 권한이 필요할 수 있음. 필요 시 서명 URL로 대체 가능.
    url = blob.public_url
    size = blob.size or 0
    return path, url, size

# Firestore 헬퍼
def fs_add(collection: str, data: Dict[str, Any]) -> str:
    doc_ref = fs.collection(collection).document()
    data.setdefault("created_at", SERVER_TIMESTAMP)
    data["updated_at"] = SERVER_TIMESTAMP
    doc_ref.set(data)
    return doc_ref.id

def fs_update(collection: str, doc_id: str, data: Dict[str, Any]):
    data["updated_at"] = SERVER_TIMESTAMP
    fs.collection(collection).document(doc_id).set(data, merge=True)


def fs_delete(collection: str, doc_id: str):
    fs.collection(collection).document(doc_id).delete()


@st.cache_data(ttl=20, show_spinner=False)
def get_subjects(year: int, term: str) -> List[Dict[str, Any]]:
    q = fs.collection(COL_SUBJECTS).where("year", "==", int(year)).where("term", "==", str(term))
    docs = q.stream()
    rows = []
    for d in docs:
        r = d.to_dict()
        r["id"] = d.id
        rows.append(r)
    rows.sort(key=lambda x: (x.get("name","")))
    return rows

@st.cache_data(ttl=20, show_spinner=False)
def get_classes(year: int, term: str) -> List[Dict[str, Any]]:
    q = fs.collection(COL_CLASSES).where("year", "==", int(year)).where("term", "==", str(term))
    docs = q.stream()
    rows = []
    for d in docs:
        x = d.to_dict(); x["id"] = d.id
        rows.append(x)
    rows.sort(key=lambda x: (x.get("class_name","")))
    return rows

@st.cache_data(ttl=20, show_spinner=False)
def get_students(class_id: str) -> List[Dict[str, Any]]:
    q = fs.collection(COL_STUDENTS).where("class_id", "==", class_id)
    docs = q.stream()
    rows = []
    for d in docs:
        x = d.to_dict(); x["id"] = d.id
        rows.append(x)
    rows.sort(key=lambda x: (x.get("student_no","")))
    return rows

@st.cache_data(ttl=20, show_spinner=False)
def get_lesson_logs_by_date(date_str: str) -> List[Dict[str, Any]]:
    q = fs.collection(COL_LESSON_LOGS).where("date", "==", date_str)
    docs = q.stream()
    rows = []
    for d in docs:
        x = d.to_dict(); x["id"] = d.id
        rows.append(x)
    rows.sort(key=lambda x: (x.get("class_name",""), x.get("period",0)))
    return rows

@st.cache_data(ttl=20, show_spinner=False)
def get_attendance_by_date(date_str: str) -> List[Dict[str, Any]]:
    q = fs.collection(COL_ATTENDANCE).where("date", "==", date_str)
    docs = q.stream()
    rows = []
    for d in docs:
        x = d.to_dict(); x["id"] = d.id
        rows.append(x)
    rows.sort(key=lambda x: (x.get("class_id",""), x.get("period",0), x.get("student_no","")))
    return rows

# 캐시 무효화 유틸
def invalidate_subjects():
    get_subjects.clear()

def invalidate_classes():
    get_classes.clear()

def invalidate_students():
    get_students.clear()

def invalidate_lesson_logs():
    get_lesson_logs_by_date.clear()

def invalidate_attendance():
    get_attendance_by_date.clear()

# ------------------------------
# 전역 필터(사이드바)
# ------------------------------
year = st.sidebar.number_input("학년도", min_value=2000, max_value=2100, value=datetime.now().year, step=1, key="filter_year")
term = st.sidebar.selectbox("학기", ["1", "2"], index=0, key="filter_term")
menu = st.sidebar.selectbox(
    "메뉴",
    [
        "담당 교과 관리",
        "수업(반) 관리",
        "시간표(요일·교시) 설정",
        "학생 관리 (반별)",
        "진도·특기사항 관리 (반별)",
        "일자별 진도·특기사항 조회 (전체 반)",
        "출결 관리 (반·학생·일자별)",
        "일자별 출결 조회 (전체 반)",
        "스프레드시트 내보내기",
        "설정/초기 점검",
    ],
    index=0,
)

st.title("수업·평가 관리")
st.caption("앱 시간대: Asia/Seoul · 오늘: " + today_str())

# ------------------------------
# 공통 컴포넌트
# ------------------------------

def subject_selectbox(label: str, year: int, term: str) -> Optional[Dict[str, Any]]:
    subjects = get_subjects(year, term)
    if not subjects:
        st.info("등록된 교과가 없습니다.")
        return None
    opts = {f"{s['name']} (Y{s['year']} T{s['term']})": s for s in subjects}
    key = st.selectbox(label, list(opts.keys()))
    return opts.get(key)


def class_selectbox(label: str, year: int, term: str) -> Optional[Dict[str, Any]]:
    classes = get_classes(year, term)
    if not classes:
        st.info("등록된 수업(반)이 없습니다.")
        return None
    opts = {f"{c['class_name']} · {c.get('subject_name','?')}": c for c in classes}
    key = st.selectbox(label, list(opts.keys()))
    return opts.get(key)


# ------------------------------
# 3.1 담당 교과 관리
# ------------------------------

def render_subjects():
    st.header("담당 교과 관리")
    subjects = get_subjects(year, term)
    col_new, _ = st.columns([1,4])
    with col_new:
        if st.button("+ 교과 등록", use_container_width=True):
            open_subject_dialog(None)

    if not subjects:
        st.info("등록된 교과가 없습니다. 우측 상단의 '+ 교과 등록' 버튼을 클릭하여 등록하세요.")
        return

    df = pd.DataFrame([
        {
            "교과명": s.get("name",""),
            "학년도": s.get("year",""),
            "학기": s.get("term",""),
            "PDF": "있음" if s.get("pdf_path") else "없음",
            "등록일": s.get("created_at"),
            "수정": "수정",
            "삭제": "삭제",
            "업로드/보기": "업로드/보기",
            "_id": s.get("id"),
        }
        for s in subjects
    ])
    st.dataframe(df[["교과명","학년도","학기","PDF","등록일"]], use_container_width=True)

    # 액션 버튼들(행 단위)
    for s in subjects:
        c1, c2, c3 = st.columns(3)
        with c1:
            if st.button(f"수정: {s['name']}", key=f"sub_edit_{s['id']}"):
                open_subject_dialog(s)
        with c2:
            if st.button(f"PDF 업로드/보기: {s['name']}", key=f"sub_pdf_{s['id']}"):
                open_subject_pdf_dialog(s)
        with c3:
            if st.button(f"삭제: {s['name']}", key=f"sub_del_{s['id']}"):
                try:
                    fs_delete(COL_SUBJECTS, s['id'])
                    invalidate_subjects()
                    st.success("삭제되었습니다.")
                    rerun()
                except Exception as e:
                    st.error(f"삭제 실패: {e}")


@st.dialog("교과 등록/수정")
def open_subject_dialog(existing: Optional[Dict[str, Any]]):
    name = st.text_input("교과명", value=existing.get("name","") if existing else "")
    y = st.number_input("학년도", min_value=2000, max_value=2100, value=int(existing.get("year", year) if existing else year))
    t = st.selectbox("학기", ["1","2"], index=(0 if (existing and str(existing.get("term"))=="1") or not existing else 1))

    if st.button("저장", type="primary"):
        if not name.strip():
            st.error("교과명을 입력하세요.")
            return
        data = {
            "name": name.strip(),
            "year": int(y),
            "term": str(t),
        }
        try:
            if existing:
                fs_update(COL_SUBJECTS, existing["id"], data)
            else:
                sid = fs_add(COL_SUBJECTS, data)
            invalidate_subjects()
            st.toast("저장되었습니다.")
            rerun()
        except Exception as e:
            st.error(f"저장 실패: {e}")


@st.dialog("교과 PDF 업로드/보기")
def open_subject_pdf_dialog(existing: Dict[str, Any]):
    st.write(f"**교과명:** {existing.get('name')} · **학년도/학기:** {existing.get('year')}/{existing.get('term')}")
    if existing.get("pdf_url"):
        st.markdown(f"현재 PDF: [열기]({existing['pdf_url']})")
    uploaded = st.file_uploader("PDF 업로드", type=["pdf"], accept_multiple_files=False)
    if st.button("저장", type="primary"):
        ok, msg = validate_pdf(uploaded)
        if not ok:
            st.error(msg)
            return
        try:
            path, url, size = upload_pdf_to_storage(existing['id'], uploaded)
            fs_update(COL_SUBJECTS, existing['id'], {
                "pdf_path": path,
                "pdf_url": url,
                "pdf_size": int(size or 0),
                "pdf_mime": "application/pdf",
            })
            invalidate_subjects()
            st.toast("PDF 업로드 완료")
            rerun()
        except Exception as e:
            st.error(f"업로드 실패: {e}")


# ------------------------------
# 3.2 수업(반) 관리 & 3.3 시간표 설정
# ------------------------------

def render_classes():
    st.header("수업(반) 관리")
    subjects = get_subjects(year, term)
    if not subjects:
        st.info("반을 등록하려면 먼저 담당 교과를 등록하세요.")
    if st.button("+ 수업(반) 등록", use_container_width=True):
        open_class_dialog(None, subjects)

    classes = get_classes(year, term)
    if not classes:
        st.info("등록된 수업(반)이 없습니다.")
        return

    df = pd.DataFrame([
        {
            "반명": c.get("class_name",""),
            "교과": c.get("subject_name",""),
            "학년도": c.get("year",""),
            "학기": c.get("term",""),
            "학생수": c.get("student_count", 0),
            "_id": c.get("id"),
        } for c in classes
    ])
    st.dataframe(df[["반명","교과","학년도","학기","학생수"]], use_container_width=True)

    for c in classes:
        cc1, cc2, cc3 = st.columns(3)
        with cc1:
            if st.button(f"수정: {c['class_name']}", key=f"cls_edit_{c['id']}"):
                open_class_dialog(c, subjects)
        with cc2:
            if st.button(f"시간표 설정: {c['class_name']}", key=f"cls_sched_{c['id']}"):
                open_schedule_dialog(c)
        with cc3:
            if st.button(f"삭제: {c['class_name']}", key=f"cls_del_{c['id']}"):
                try:
                    # 종속 데이터 삭제(학생, 진도, 출결)
                    delete_class_cascade(c['id'])
                    fs_delete(COL_CLASSES, c['id'])
                    invalidate_classes(); invalidate_students(); invalidate_lesson_logs(); invalidate_attendance()
                    st.success("반과 종속 데이터가 삭제되었습니다.")
                    rerun()
                except Exception as e:
                    st.error(f"삭제 실패: {e}")


def delete_class_cascade(class_id: str):
    # 학생
    studs = fs.collection(COL_STUDENTS).where("class_id","==",class_id).stream()
    for d in studs:
        d.reference.delete()
    # 진도
    logs = fs.collection(COL_LESSON_LOGS).where("class_id","==",class_id).stream()
    for d in logs:
        d.reference.delete()
    # 출결
    atts = fs.collection(COL_ATTENDANCE).where("class_id","==",class_id).stream()
    for d in atts:
        d.reference.delete()


@st.dialog("수업(반) 등록/수정")
def open_class_dialog(existing: Optional[Dict[str,Any]], subjects: List[Dict[str,Any]]):
    if not subjects:
        st.info("먼저 교과를 등록하세요.")
        return
    sub_opts = {f"{s['name']}": s for s in subjects}
    default_idx = 0
    if existing:
        # find index
        names = list(sub_opts.keys())
        try:
            default_idx = names.index(existing.get("subject_name",""))
        except Exception:
            default_idx = 0
    sub_name = st.selectbox("교과 선택", list(sub_opts.keys()), index=default_idx)
    class_name = st.text_input("수업 학반명(예: 1-3)", value=existing.get("class_name","") if existing else "")

    y = st.number_input("학년도", min_value=2000, max_value=2100, value=int(existing.get("year", year) if existing else year))
    t = st.selectbox("학기", ["1","2"], index=(0 if (existing and str(existing.get("term"))=="1") or not existing else 1))

    if st.button("저장", type="primary"):
        if not class_name.strip():
            st.error("학반명을 입력하세요.")
            return
        s = sub_opts[sub_name]
        payload = {
            "subject_id": s['id'],
            "subject_name": s['name'],
            "year": int(y),
            "term": str(t),
            "class_name": class_name.strip(),
        }
        try:
            if existing:
                fs_update(COL_CLASSES, existing['id'], payload)
            else:
                fs_add(COL_CLASSES, payload)
            invalidate_classes()
            st.toast("저장되었습니다.")
            rerun()
        except Exception as e:
            st.error(f"저장 실패: {e}")


@st.dialog("시간표(요일·교시) 설정")
def open_schedule_dialog(class_doc: Dict[str,Any]):
    st.write(f"**반:** {class_doc.get('class_name')} · **교과:** {class_doc.get('subject_name')}")
    existing = class_doc.get("schedule", [])
    df = pd.DataFrame(existing) if existing else pd.DataFrame(columns=["weekday","period"])  # weekday:int(1~7), period:int
    edited = st.data_editor(
        df,
        num_rows="dynamic",
        use_container_width=True,
        column_config={
            "weekday": st.column_config.NumberColumn("요일(1~7)", min_value=1, max_value=7, step=1),
            "period": st.column_config.NumberColumn("교시", min_value=1, step=1),
        },
        key=f"sched_edit_{class_doc['id']}"
    )
    if st.button("저장", type="primary"):
        # 유효성 & 중복 검사
        try:
            rows = edited.fillna(0).astype({"weekday":int,"period":int}).to_dict("records") if not edited.empty else []
            seen = set()
            valid_rows = []
            for r in rows:
                w, p = int(r.get("weekday",0)), int(r.get("period",0))
                if not (1 <= w <= 7 and p >= 1):
                    continue
                key = (w,p)
                if key in seen:
                    continue
                seen.add(key)
                valid_rows.append({"weekday":w, "period":p})
            fs_update(COL_CLASSES, class_doc['id'], {"schedule": valid_rows})
            invalidate_classes()
            st.toast("시간표가 저장되었습니다.")
            rerun()
        except Exception as e:
            st.error(f"저장 실패: {e}")


# ------------------------------
# 3.4 학생 관리 (반별)
# ------------------------------

def render_students():
    st.header("학생 관리 (반별)")
    cls = class_selectbox("반 선택", year, term)
    if not cls:
        return

    st.subheader(f"학생 목록 · {cls['class_name']}")
    if st.button("+ 학생 추가", use_container_width=True):
        open_student_dialog(cls, None)

    # CSV 업로드
    with st.expander("CSV 업로드(학번,성명)"):
        st.caption("CSV 예시: 첫 줄 헤더 '학번,성명' · 인코딩 UTF-8 또는 CP949")
        sample = "학번,성명\n20250101,홍길동\n20250102,김철수\n"
        st.download_button("CSV 템플릿 다운로드", data=sample.encode("utf-8"), file_name="students_template.csv", mime="text/csv")
        up = st.file_uploader("CSV 선택", type=["csv"], accept_multiple_files=False)
        strategy = st.selectbox("중복 정책", ["업서트(학번 기준 갱신)", "건너뛰기"])
        if st.button("업로드 실행", type="primary"):
            if up is None:
                st.error("CSV 파일을 선택하세요.")
            else:
                try:
                    text = up.read()
                    for enc in ("utf-8-sig","utf-8","cp949"):
                        try:
                            df = pd.read_csv(io.BytesIO(text), encoding=enc)
                            break
                        except Exception:
                            df = None
                    if df is None:
                        st.error("CSV 파싱 실패(인코딩 확인)")
                    else:
                        if set(df.columns) != set(["학번","성명"]):
                            st.error("헤더가 '학번,성명' 이어야 합니다.")
                        else:
                            upsert = (strategy.startswith("업서트"))
                            ok, fail, skip = bulk_import_students(cls['id'], df, upsert)
                            invalidate_students()
                            st.success(f"업로드 완료: 성공 {ok}, 실패 {fail}, 건너뜀 {skip}")
                            rerun()
                except Exception as e:
                    st.error(f"업로드 오류: {e}")

    # 목록 표시
    students = get_students(cls['id'])
    if not students:
        st.info("이 반의 학생 정보가 없습니다.")
        return
    df = pd.DataFrame([{ "학번": s.get("student_no"), "성명": s.get("student_name"), "_id": s["id"] } for s in students])
    st.dataframe(df[["학번","성명"]], use_container_width=True)

    for s in students:
        c1, c2 = st.columns(2)
        with c1:
            if st.button(f"수정: {s['student_no']} {s['student_name']}", key=f"stu_edit_{s['id']}"):
                open_student_dialog(cls, s)
        with c2:
            if st.button(f"삭제: {s['student_no']}", key=f"stu_del_{s['id']}"):
                try:
                    fs_delete(COL_STUDENTS, s['id'])
                    # 종속 출결 삭제(옵션)
                    atts = fs.collection(COL_ATTENDANCE).where("student_id","==",s['id']).stream()
                    for d in atts: d.reference.delete()
                    invalidate_students(); invalidate_attendance()
                    st.success("삭제되었습니다.")
                    rerun()
                except Exception as e:
                    st.error(f"삭제 실패: {e}")


def bulk_import_students(class_id: str, df: pd.DataFrame, upsert: bool) -> Tuple[int,int,int]:
    ok = fail = skip = 0
    for _, row in df.iterrows():
        no = str(row.get("학번",""))
        nm = str(row.get("성명","")).strip()
        if not no or not nm:
            fail += 1; continue
        # 기존 존재?
        q = fs.collection(COL_STUDENTS).where("class_id","==",class_id).where("student_no","==",no).limit(1).stream()
        found = None
        for d in q:
            found = d
        try:
            if found:
                if upsert:
                    fs_update(COL_STUDENTS, found.id, {"student_name": nm})
                    ok += 1
                else:
                    skip += 1
            else:
                fs_add(COL_STUDENTS, {"class_id": class_id, "student_no": no, "student_name": nm})
                ok += 1
        except Exception:
            fail += 1
    return ok, fail, skip


@st.dialog("학생 등록/수정")
def open_student_dialog(cls: Dict[str,Any], existing: Optional[Dict[str,Any]]):
    st.write(f"**반:** {cls['class_name']}")
    no = st.text_input("학번", value=existing.get("student_no","") if existing else "")
    nm = st.text_input("성명", value=existing.get("student_name","") if existing else "")
    if st.button("저장", type="primary"):
        if not no.strip() or not nm.strip():
            st.error("학번과 성명을 입력하세요.")
            return
        try:
            if existing:
                fs_update(COL_STUDENTS, existing['id'], {"student_no": no.strip(), "student_name": nm.strip()})
            else:
                # 중복 검사
                q = fs.collection(COL_STUDENTS).where("class_id","==",cls['id']).where("student_no","==",no.strip()).limit(1).stream()
                exists = any(True for _ in q)
                if exists:
                    st.error("이미 해당 학번이 존재합니다.")
                    return
                fs_add(COL_STUDENTS, {"class_id": cls['id'], "student_no": no.strip(), "student_name": nm.strip()})
            invalidate_students()
            st.toast("저장되었습니다.")
            rerun()
        except Exception as e:
            st.error(f"저장 실패: {e}")


# ------------------------------
# 3.5 진도·특기사항 관리 (반별)
# ------------------------------

def render_lesson_logs():
    st.header("진도·특기사항 관리 (반별)")
    cls = class_selectbox("반 선택", year, term)
    if not cls:
        return
    date_val = st.date_input("일자", value=datetime.now())
    period = st.number_input("교시", min_value=1, value=1, step=1)

    # 목록 표시
    date_str = date_val.strftime("%Y-%m-%d")
    q = fs.collection(COL_LESSON_LOGS).where("class_id","==",cls['id']).where("date","==",date_str).stream()
    rows = []
    for d in q:
        x = d.to_dict(); x['id'] = d.id; rows.append(x)
    rows.sort(key=lambda x: x.get("period",0))

    st.subheader(f"{cls['class_name']} · {date_str} 기록")
    if not rows:
        st.info("기록된 진도/특기사항이 없습니다.")
    else:
        df = pd.DataFrame([{ "교시": r.get("period"), "진도": r.get("progress",""), "특기사항": r.get("note",""), "_id": r['id']} for r in rows])
        st.dataframe(df[["교시","진도","특기사항"]], use_container_width=True)

    c1, c2 = st.columns(2)
    with c1:
        if st.button("+ 기록 추가", use_container_width=True):
            open_log_dialog(cls, None, date_str, int(period))
    with c2:
        if rows:
            # 첫 행 수정 예시 버튼
            for r in rows:
                if st.button(f"수정: {r['period']}교시", key=f"log_edit_{r['id']}"):
                    open_log_dialog(cls, r, date_str, int(period))


@st.dialog("진도·특기사항 등록/수정")
def open_log_dialog(cls: Dict[str,Any], existing: Optional[Dict[str,Any]], date_default: str, period_default: int):
    date_str = st.text_input("일자(YYYY-MM-DD)", value=(existing.get("date", date_default) if existing else date_default))
    period = st.number_input("교시", min_value=1, value=int(existing.get("period", period_default) if existing else period_default), step=1)
    progress = st.text_area("진도", value=existing.get("progress","") if existing else "")
    note = st.text_area("특기사항", value=existing.get("note","") if existing else "")

    if st.button("저장", type="primary"):
        if not date_str:
            st.error("일자를 입력하세요.")
            return
        payload = {
            "class_id": cls['id'],
            "class_name": cls.get('class_name',''),
            "subject_id": cls.get('subject_id',''),
            "date": date_str,
            "period": int(period),
            "progress": progress.strip(),
            "note": note.strip(),
        }
        try:
            # 중복키: class_id+date+period
            q = fs.collection(COL_LESSON_LOGS).where("class_id","==",cls['id']).where("date","==",date_str).where("period","==",int(period)).limit(1).stream()
            dup = None
            for d in q: dup = d
            if existing:
                fs_update(COL_LESSON_LOGS, existing['id'], payload)
            else:
                if dup:
                    fs_update(COL_LESSON_LOGS, dup.id, payload)
                else:
                    fs_add(COL_LESSON_LOGS, payload)
            invalidate_lesson_logs()
            st.toast("저장되었습니다.")
            rerun()
        except Exception as e:
            st.error(f"저장 실패: {e}")


# ------------------------------
# 3.6 일자별 진도·특기사항 조회 (전체 반)
# ------------------------------

def render_daily_logs():
    st.header("일자별 진도·특기사항 조회 (전체 반)")
    date_val = st.date_input("일자", value=datetime.now())
    date_str = date_val.strftime("%Y-%m-%d")
    rows = get_lesson_logs_by_date(date_str)
    if not rows:
        st.info("선택한 일자에 대한 진도/특기사항이 없습니다.")
        return
    df = pd.DataFrame([
        {
            "반": r.get("class_name",""),
            "교과": r.get("subject_id",""),  # subject_name을 저장하려면 denorm 확장 가능
            "교시": r.get("period",0),
            "진도": r.get("progress",""),
            "특기사항": r.get("note",""),
        } for r in rows
    ])
    st.dataframe(df.sort_values(["반","교시"]), use_container_width=True)


# ------------------------------
# 3.7 출결 관리 (반·학생·일자별)
# ------------------------------

def render_attendance():
    st.header("출결 관리 (반·학생·일자별)")
    cls = class_selectbox("반 선택", year, term)
    if not cls:
        return
    date_val = st.date_input("일자", value=datetime.now())
    period = st.number_input("교시", min_value=1, value=1, step=1)

    if st.button("출결 입력/수정", type="primary"):
        open_attendance_dialog(cls, date_val.strftime("%Y-%m-%d"), int(period))


@st.dialog("출결 입력/수정")
def open_attendance_dialog(cls: Dict[str,Any], date_str: str, period: int):
    st.write(f"**반:** {cls['class_name']} · **일자:** {date_str} · **교시:** {period}")
    students = get_students(cls['id'])
    if not students:
        st.info("학생 명부가 없습니다. 학생을 먼저 등록하세요.")
        return
    # 기존 출결 로드
    q = fs.collection(COL_ATTENDANCE).where("class_id","==",cls['id']).where("date","==",date_str).where("period","==",int(period)).stream()
    old = {(d.to_dict().get("student_id")): {**d.to_dict(), "id": d.id} for d in q}

    # 편집용 DF
    rows = []
    for s in students:
        cur = old.get(s['id'], {})
        rows.append({
            "student_id": s['id'],
            "학번": s.get("student_no",""),
            "성명": s.get("student_name",""),
            "상태": cur.get("status","present"),
            "특기사항": cur.get("remark",""),
        })
    df = pd.DataFrame(rows)
    edited = st.data_editor(
        df,
        num_rows="fixed",
        use_container_width=True,
        column_config={
            "student_id": st.column_config.TextColumn("student_id", disabled=True),
            "학번": st.column_config.TextColumn("학번", disabled=True),
            "성명": st.column_config.TextColumn("성명", disabled=True),
            "상태": st.column_config.SelectboxColumn("상태", options=ATTENDANCE_STATES),
            "특기사항": st.column_config.TextColumn("특기사항"),
        },
        hide_index=True,
        key=f"att_edit_{cls['id']}_{date_str}_{period}"
    )

    # 일괄 설정
    with st.expander("일괄 입력"):
        state = st.selectbox("전체 상태 설정", ATTENDANCE_STATES)
        if st.button("전체 적용"):
            edited["상태"] = state
            st.toast("전체 상태가 적용되었습니다. 필요 시 개별 행을 수정한 뒤 저장하세요.")

    if st.button("저장", type="primary"):
        try:
            # 행 단위 저장
            for _, r in edited.iterrows():
                sid = r["student_id"]
                payload = {
                    "class_id": cls['id'],
                    "student_id": sid,
                    "student_no": str(r["학번"]),
                    "student_name": str(r["성명"]),
                    "date": date_str,
                    "period": int(period),
                    "status": str(r["상태"]),
                    "remark": str(r.get("특기사항","")),
                }
                # upsert by (class_id,date,period,student_id)
                existing = old.get(sid)
                if existing:
                    fs_update(COL_ATTENDANCE, existing['id'], payload)
                else:
                    fs_add(COL_ATTENDANCE, payload)
            invalidate_attendance()
            st.toast("출결이 저장되었습니다.")
            rerun()
        except Exception as e:
            st.error(f"저장 실패: {e}")


# ------------------------------
# 3.8 일자별 출결 조회 (전체 반)
# ------------------------------

def render_daily_attendance():
    st.header("일자별 출결 조회 (전체 반)")
    date_val = st.date_input("일자", value=datetime.now())
    date_str = date_val.strftime("%Y-%m-%d")
    rows = get_attendance_by_date(date_str)
    if not rows:
        st.info("선택한 일자에 대한 출결 데이터가 없습니다.")
        return
    # 요약
    summary = pd.Series([r.get("status","present") for r in rows]).value_counts()
    s1, s2, s3, s4 = st.columns(4)
    s1.metric("출석", int(summary.get("present",0)))
    s2.metric("결석", int(summary.get("absent",0)))
    s3.metric("지각", int(summary.get("late",0)))
    s4.metric("공결", int(summary.get("excused",0)))

    df = pd.DataFrame([
        {
            "반": r.get("class_id",""),
            "교시": r.get("period",0),
            "학번": r.get("student_no",""),
            "성명": r.get("student_name",""),
            "상태": r.get("status",""),
            "특기사항": r.get("remark",""),
        } for r in rows
    ])
    st.dataframe(df.sort_values(["반","교시","학번"]), use_container_width=True)


# ------------------------------
# 3.9 스프레드시트 내보내기
# ------------------------------

def render_export():
    st.header("스프레드시트 내보내기")
    collections = [COL_SUBJECTS, COL_CLASSES, COL_STUDENTS, COL_LESSON_LOGS, COL_ATTENDANCE]
    selected = st.multiselect("내보낼 컬렉션 선택", collections, default=collections)
    default_title = f"Firestore Export {datetime.now().strftime('%Y%m%d-%H%M')}"
    title = st.text_input("스프레드시트 제목", value=default_title)
    if st.button("내보내기 실행", type="primary"):
        if not selected:
            st.error("컬렉션을 선택하세요.")
        else:
            try:
                url = export_to_gsheet(selected, title)
                # 로그 저장(선택)
                fs_add(COL_EXPORTS, {"title": title, "collections": selected, "spreadsheet_url": url})
                st.success("내보내기 완료")
                st.markdown(f"스프레드시트 열기: [{url}]({url})")
            except Exception as e:
                st.error(f"내보내기 실패: {e}")


def export_to_gsheet(collections: List[str], title: str) -> str:
    # gspread 인증: 서비스 계정 정보 사용
    creds = service_account.Credentials.from_service_account_info(FIREBASE_INFO, scopes=[
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ])
    gc = gspread.authorize(creds)
    sh = gc.create(title)
    # 각 컬렉션을 별도 워크시트로
    for col in collections:
        ws = None
        # gspread 기본 시트가 하나 존재 -> 첫 시트를 재사용 또는 새 시트 추가
        try:
            ws = sh.add_worksheet(title=col, rows=1, cols=1)
        except Exception:
            # 동일 이름 시트가 있으면 고유 이름 보정
            ws = sh.add_worksheet(title=f"{col}_{int(time.time())}", rows=1, cols=1)
        # 데이터 로드
        docs = fs.collection(col).stream()
        rows = []
        for d in docs:
            x = d.to_dict(); x["id"] = d.id; rows.append(x)
        if rows:
            df = pd.DataFrame(rows)
            values = [list(df.columns)] + df.fillna("").astype(str).values.tolist()
            ws.update("A1", values)
        else:
            ws.update("A1", [["(empty)"]])
    # 기본 첫 시트가 비어 있으면 삭제
    try:
        default_ws = sh.sheet1
        if default_ws.title not in collections:
            sh.del_worksheet(default_ws)
    except Exception:
        pass
    # 서비스 계정 소유. 필요 시 공유는 별도 처리. 여기서는 URL만 반환
    return sh.url


# ------------------------------
# 3.10 설정/초기 점검
# ------------------------------

def render_settings():
    st.header("설정/초기 점검")
    st.write("**Project ID:**", FIREBASE_INFO.get("project_id"))
    st.write("**Storage Bucket:**", FIREBASE_INFO.get("storageBucket"))
    st.write("**Service Account:**", FIREBASE_INFO.get("client_email"))

    if st.button("Firestore 연결 점검"):
        try:
            _ = list(fs.collections())
            st.success("Firestore 연결 OK")
        except Exception as e:
            st.error(f"Firestore 연결 실패: {e}")

    if st.button("Storage 연결 점검"):
        try:
            _ = list(bucket.list_blobs(max_results=1))
            st.success("Storage 연결 OK")
        except Exception as e:
            st.error(f"Storage 연결 실패: {e}")

    with st.expander("CSV 템플릿 다운로드"):
        sample = "학번,성명\n20250101,홍길동\n20250102,김철수\n"
        st.download_button("학생 CSV 템플릿", data=sample.encode("utf-8"), file_name="students_template.csv", mime="text/csv")


# ------------------------------
# 라우팅
# ------------------------------
if menu == "담당 교과 관리":
    render_subjects()
elif menu == "수업(반) 관리":
    render_classes()
elif menu == "시간표(요일·교시) 설정":
    # 시간표 설정은 수업(반) 관리에서 각 반의 버튼으로 진입하도록 구성했으나,
    # 이 메뉴에서는 안내만 제공합니다.
    st.header("시간표(요일·교시) 설정")
    st.info("수업(반) 관리에서 각 반의 '시간표 설정' 버튼을 눌러 편집하세요.")
elif menu == "학생 관리 (반별)":
    render_students()
elif menu == "진도·특기사항 관리 (반별)":
    render_lesson_logs()
elif menu == "일자별 진도·특기사항 조회 (전체 반)":
    render_daily_logs()
elif menu == "출결 관리 (반·학생·일자별)":
    render_attendance()
elif menu == "일자별 출결 조회 (전체 반)":
    render_daily_attendance()
elif menu == "스프레드시트 내보내기":
    render_export()
elif menu == "설정/초기 점검":
    render_settings()

# 끝
