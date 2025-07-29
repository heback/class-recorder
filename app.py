# app.py
import streamlit as st
import firebase_admin
from firebase_admin import credentials, firestore, storage
import pandas as pd
import gspread
from gspread_dataframe import set_with_dataframe
from google.oauth2.service_account import Credentials
from datetime import datetime
import io
import uuid

# --- 1. 초기 설정 및 Firebase/Gspread 연동 ---

# Streamlit 페이지 설정
st.set_page_config(page_title="교사용 수업 관리 시스템", layout="wide")

# Firebase 서비스 계정 키 및 Gspread 키 로드 (st.secrets 사용)
try:
    firebase_creds_dict = dict(st.secrets["FIREBASE_KEY"])
    gspread_creds_dict = dict(st.secrets["GSPREAD_KEY"])

    # Gspread 인증 범위 설정
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    gspread_credentials = Credentials.from_service_account_info(
        gspread_creds_dict, scopes=scopes
    )
    gc = gspread.authorize(gspread_credentials)

except (KeyError, FileNotFoundError):
    st.error(
        "필수 인증 정보(FIREBASE_KEY 또는 GSPREAD_KEY)를 찾을 수 없습니다. Streamlit Secrets를 확인하세요.")
    st.stop()


# Firebase 앱 초기화 함수
def initialize_firebase():
    """
    Firebase 앱이 초기화되지 않았을 경우 초기화합니다.
    st.secrets에서 인증 정보를 가져와 사용합니다.
    """
    try:
        if not firebase_admin._apps:
            cred = credentials.Certificate(firebase_creds_dict)
            firebase_admin.initialize_app(cred, {
                'storageBucket': firebase_creds_dict.get('storageBucket')
            })
        return firestore.client()
    except Exception as e:
        st.error(f"Firebase 초기화 중 오류 발생: {e}")
        st.stop()


# Firestore 클라이언트 초기화
db = initialize_firebase()


# --- 2. 헬퍼 함수 (데이터베이스 및 스토리지 CRUD) ---

# PDF 파일 업로드
def upload_pdf_to_storage(file_object, destination_blob_name):
    """Firebase Storage에 PDF 파일을 업로드하고 공개 URL과 파일 경로를 반환합니다."""
    try:
        bucket = storage.bucket()
        blob = bucket.blob(destination_blob_name)

        # 파일 포인터를 처음으로 되돌림
        file_object.seek(0)

        blob.upload_from_file(file_object, content_type='application/pdf')

        # 파일에 공개 접근 권한 부여
        blob.make_public()

        return blob.public_url, destination_blob_name
    except Exception as e:
        st.error(f"파일 업로드 실패: {e}")
        return None, None


# PDF 파일 삭제
def delete_pdf_from_storage(blob_name):
    """Firebase Storage에서 PDF 파일을 삭제합니다."""
    if not blob_name:
        return
    try:
        bucket = storage.bucket()
        blob = bucket.blob(blob_name)
        if blob.exists():
            blob.delete()
    except Exception as e:
        st.warning(f"Storage 파일 삭제 중 오류 발생: {e}")


# --- 3. 메뉴별 기능 구현 ---

# 3.1. 교과 관리
@st.dialog("교과 정보")
def course_dialog(course_id=None):
    """교과 추가 또는 수정을 위한 다이얼로그 함수"""
    is_edit = course_id is not None
    title = "교과 수정" if is_edit else "새 교과 추가"
    st.subheader(title)

    default_data = {}
    if is_edit:
        doc_ref = db.collection("courses").document(course_id)
        doc = doc_ref.get()
        if doc.exists:
            default_data = doc.to_dict()

    with st.form("course_form"):
        year = st.number_input("학년도", min_value=2020, max_value=2050,
                               value=default_data.get("year",
                                                      datetime.now().year))
        semester = st.selectbox("학기", [1, 2], index=[1, 2].index(
            default_data.get("semester", 1)))
        name = st.text_input("교과명", value=default_data.get("name", ""))
        uploaded_file = st.file_uploader("수업계획서 (PDF, 10MB 이하)", type="pdf")

        submitted = st.form_submit_button("저장")
        if submitted:
            if not name:
                st.warning("교과명을 입력해주세요.")
            else:
                data = {"year": year, "semester": semester, "name": name}

                if uploaded_file is not None:
                    if uploaded_file.size > 10 * 1024 * 1024:
                        st.error("파일 크기가 10MB를 초과할 수 없습니다.")
                        return

                    if is_edit and default_data.get("pdf_path"):
                        delete_pdf_from_storage(default_data["pdf_path"])

                    file_path = f"plans/{uuid.uuid4()}_{uploaded_file.name}"
                    pdf_url, pdf_path = upload_pdf_to_storage(uploaded_file,
                                                              file_path)
                    if pdf_url:
                        data["pdf_url"] = pdf_url
                        data["pdf_path"] = pdf_path

                if is_edit:
                    db.collection("courses").document(course_id).update(data)
                    st.success("교과 정보가 수정되었습니다.")
                else:
                    data["created_at"] = firestore.SERVER_TIMESTAMP
                    db.collection("courses").add(data)
                    st.success("새 교과가 추가되었습니다.")

                st.rerun()


def course_management():
    st.header("📚 교과 관리")
    st.markdown("담당 교과의 수업 및 평가 계획을 관리합니다.")

    if st.button("➕ 새 교과 추가", type="primary"):
        course_dialog()

    st.subheader("등록된 교과 목록")
    courses_ref = db.collection("courses").order_by("year",
                                                    direction=firestore.Query.DESCENDING).order_by(
        "semester", direction=firestore.Query.DESCENDING).stream()
    courses_list = list(courses_ref)

    if not courses_list:
        st.info("등록된 교과가 없습니다. '새 교과 추가' 버튼을 눌러 추가해주세요.")
    else:
        for course in courses_list:
            c = course.to_dict()
            with st.container(border=True):
                col1, col2, col3, col4, col5 = st.columns([3, 3, 1, 1, 1])
                with col1:
                    st.markdown(f"**{c.get('name', '이름 없음')}**")
                with col2:
                    st.markdown(f"_{c.get('year')}년 {c.get('semester')}학기_")
                with col3:
                    if c.get("pdf_url"):
                        st.link_button("계획서 보기", c["pdf_url"],
                                       use_container_width=True)
                with col4:
                    if st.button("수정", key=f"edit_{course.id}",
                                 use_container_width=True):
                        course_dialog(course_id=course.id)
                with col5:
                    if st.button("삭제", key=f"delete_{course.id}",
                                 type="secondary", use_container_width=True):
                        if c.get("pdf_path"):
                            delete_pdf_from_storage(c["pdf_path"])
                        db.collection("courses").document(course.id).delete()
                        st.success(f"'{c.get('name')}' 교과가 삭제되었습니다.")
                        st.rerun()


# 3.2. 수업 관리
@st.dialog("수업 정보")
def class_dialog(courses, class_id=None):
    """수업 추가 또는 수정을 위한 다이얼로그 함수"""
    is_edit = class_id is not None
    title = "수업 수정" if is_edit else "새 수업 추가"
    st.subheader(title)

    default_data = {}
    if is_edit:
        doc = db.collection("classes").document(class_id).get()
        if doc.exists:
            default_data = doc.to_dict()

    with st.form("class_form"):
        course_ids = list(courses.keys())
        default_course_index = course_ids.index(
            default_data.get("course_id")) if default_data.get(
            "course_id") in course_ids else 0
        selected_course_id = st.selectbox("교과 선택", course_ids,
                                          format_func=lambda x: courses.get(x,
                                                                            "이름 없음"),
                                          index=default_course_index)

        class_name = st.text_input("학급명 (예: 1학년 1반)",
                                   value=default_data.get("class_name", ""))

        days = ["월", "화", "수", "목", "금"]
        default_schedule = default_data.get("schedule", [])

        schedule_data = []
        for day in days:
            periods_for_day = [item['period'] for item in default_schedule if
                               item.get('day') == day]
            selected_periods = st.multiselect(f"{day}요일 수업 교시",
                                              list(range(1, 9)),
                                              default=periods_for_day)
            for period in selected_periods:
                schedule_data.append({"day": day, "period": period})

        submitted = st.form_submit_button("저장")
        if submitted:
            if not class_name:
                st.warning("학급명을 입력해주세요.")
            else:
                course_doc_snap = db.collection("courses").document(
                    selected_course_id).get()
                course_doc = course_doc_snap.to_dict() if course_doc_snap.exists else {}

                data = {
                    "course_id": selected_course_id,
                    "course_name": courses.get(selected_course_id, "이름 없음"),
                    "year": course_doc.get("year"),
                    "semester": course_doc.get("semester"),
                    "class_name": class_name,
                    "schedule": schedule_data
                }
                if is_edit:
                    db.collection("classes").document(class_id).update(data)
                    st.success("수업 정보가 수정되었습니다.")
                else:
                    data["created_at"] = firestore.SERVER_TIMESTAMP
                    db.collection("classes").add(data)
                    st.success("새 수업이 추가되었습니다.")

                st.rerun()


def class_management():
    st.header("🏫 수업 관리")
    st.markdown("담당 교과에 대한 학급을 등록하고 관리합니다.")

    courses_ref = db.collection("courses").stream()
    courses = {doc.id: doc.to_dict().get('name', f'이름 없는 교과 (ID:{doc.id})') for
               doc in courses_ref}

    if not courses:
        st.warning("먼저 '교과 관리' 메뉴에서 교과를 추가해주세요.")
        return

    if st.button("➕ 새 수업 추가", type="primary"):
        class_dialog(courses)

    st.subheader("등록된 수업 목록")
    classes_ref = db.collection("classes").order_by("year",
                                                    direction=firestore.Query.DESCENDING).order_by(
        "semester", direction=firestore.Query.DESCENDING).stream()
    classes_list = list(classes_ref)

    if not classes_list:
        st.info("등록된 수업이 없습니다.")
    else:
        for class_doc in classes_list:
            c = class_doc.to_dict()
            with st.container(border=True):
                col1, col2, col3, col4 = st.columns([3, 4, 1, 1])
                with col1:
                    st.markdown(f"**{c.get('class_name', '이름 없음')}**")
                with col2:
                    st.markdown(
                        f"_{c.get('year')}년 {c.get('semester')}학기 / {c.get('course_name', '')}_")
                with col3:
                    if st.button("수정", key=f"edit_class_{class_doc.id}",
                                 use_container_width=True):
                        class_dialog(courses, class_id=class_doc.id)
                with col4:
                    if st.button("삭제", key=f"delete_class_{class_doc.id}",
                                 type="secondary", use_container_width=True):
                        db.collection("classes").document(class_doc.id).delete()
                        st.success(f"'{c.get('class_name')}' 수업이 삭제되었습니다.")
                        st.rerun()


# 3.3. 학생 관리
@st.dialog("학생 정보")
def student_dialog(class_id, student_id=None):
    """학생 추가 또는 수정을 위한 다이얼로그 함수"""
    is_edit = student_id is not None
    title = "학생 정보 수정" if is_edit else "학생 추가"
    st.subheader(title)

    default_data = {}
    if is_edit:
        doc = db.collection("classes").document(class_id).collection(
            "students").document(student_id).get()
        if doc.exists:
            default_data = doc.to_dict()

    with st.form("student_form"):
        student_number = st.text_input("학번",
                                       value=default_data.get("student_number",
                                                              ""))
        name = st.text_input("이름", value=default_data.get("name", ""))
        submitted = st.form_submit_button("저장")
        if submitted:
            if not student_number or not name:
                st.warning("학번과 이름을 모두 입력해주세요.")
            else:
                data = {"student_number": student_number, "name": name}
                student_collection = db.collection("classes").document(
                    class_id).collection("students")
                if is_edit:
                    student_collection.document(student_id).update(data)
                    st.success("학생 정보가 수정되었습니다.")
                else:
                    data["created_at"] = firestore.SERVER_TIMESTAMP
                    student_collection.add(data)
                    st.success("학생이 추가되었습니다.")

                st.rerun()


def student_management():
    st.header("🧑‍🎓 학생 관리")
    st.markdown("수업 반별로 학생 정보를 추가, 수정, 삭제합니다.")

    classes_ref = db.collection("classes").stream()
    classes_dict = {
        doc.id: f"{doc.to_dict().get('class_name', '이름 없음')} ({doc.to_dict().get('course_name', '')})"
        for doc in classes_ref}

    if not classes_dict:
        st.warning("먼저 '수업 관리' 메뉴에서 수업을 추가해주세요.")
        return

    selected_class_id = st.selectbox("수업 반 선택",
                                     options=list(classes_dict.keys()),
                                     format_func=lambda x: classes_dict.get(x,
                                                                            "이름 없음"))

    if selected_class_id:
        st.subheader(f"'{classes_dict.get(selected_class_id)}' 학생 목록")

        students_ref = db.collection("classes").document(
            selected_class_id).collection("students").order_by(
            "student_number").stream()
        students_list = list(students_ref)

        if not students_list:
            st.info("등록된 학생이 없습니다.")
        else:
            for student in students_list:
                s = student.to_dict()
                with st.container(border=True):
                    col1, col2, col3, col4 = st.columns([2, 3, 1, 1])
                    col1.text(s.get("student_number", "학번 없음"))
                    col2.text(s.get("name", "이름 없음"))
                    if col3.button("수정", key=f"edit_student_{student.id}",
                                   use_container_width=True):
                        student_dialog(selected_class_id, student_id=student.id)
                    if col4.button("삭제", key=f"delete_student_{student.id}",
                                   type="secondary", use_container_width=True):
                        db.collection("classes").document(
                            selected_class_id).collection("students").document(
                            student.id).delete()
                        st.success("학생 정보가 삭제되었습니다.")
                        st.rerun()

        st.divider()
        col1, col2 = st.columns(2)
        with col1:
            if st.button("🧑‍🎓 학생 직접 추가"):
                student_dialog(class_id=selected_class_id)

        with col2:
            csv_file = st.file_uploader("📄 엑셀(CSV)로 일괄 등록", type="csv")
            if csv_file is not None:
                try:
                    df = pd.read_csv(csv_file)
                    if '학번' not in df.columns or '이름' not in df.columns:
                        st.error("CSV 파일에 '학번'과 '이름' 컬럼이 필요합니다.")
                    else:
                        with st.spinner("학생 정보를 등록 중입니다..."):
                            batch = db.batch()
                            student_collection = db.collection(
                                "classes").document(
                                selected_class_id).collection("students")
                            for _, row in df.iterrows():
                                doc_ref = student_collection.document()
                                batch.set(doc_ref, {
                                    "student_number": str(row['학번']),
                                    "name": str(row['이름']),
                                    "created_at": firestore.SERVER_TIMESTAMP
                                })
                            batch.commit()
                        st.success(f"{len(df)}명의 학생 정보가 성공적으로 등록되었습니다.")
                        st.rerun()
                except Exception as e:
                    st.error(f"CSV 파일 처리 중 오류 발생: {e}")


# 3.4. 진도 관리
@st.dialog("진도 정보")
def progress_dialog(class_id, date_str, progress_id=None):
    """진도 추가 또는 수정을 위한 다이얼로그 함수"""
    is_edit = progress_id is not None
    title = "진도 수정" if is_edit else "진도 추가"
    st.subheader(title)

    default_data = {}
    if is_edit:
        doc = db.collection("classes").document(class_id).collection(
            "progress").document(progress_id).get()
        if doc.exists:
            default_data = doc.to_dict()

    with st.form("progress_form"):
        period = st.number_input("교시", min_value=1, max_value=8,
                                 value=default_data.get("period", 1))
        topic = st.text_input("학습 내용/진도", value=default_data.get("topic", ""))
        notes = st.text_area("특기사항", value=default_data.get("notes", ""))
        submitted = st.form_submit_button("저장")
        if submitted:
            if not topic:
                st.warning("학습 내용을 입력해주세요.")
            else:
                data = {"date": date_str, "period": period, "topic": topic,
                        "notes": notes}
                progress_collection = db.collection("classes").document(
                    class_id).collection("progress")
                if is_edit:
                    progress_collection.document(progress_id).update(data)
                    st.success("진도 정보가 수정되었습니다.")
                else:
                    data["created_at"] = firestore.SERVER_TIMESTAMP
                    progress_collection.add(data)
                    st.success("진도가 추가되었습니다.")

                st.rerun()


def progress_management():
    st.header("📈 진도 관리")
    st.markdown("수업 반별로 일자, 교시, 진도와 특기사항을 관리합니다.")

    classes_ref = db.collection("classes").stream()
    classes_dict = {
        doc.id: f"{doc.to_dict().get('class_name', '이름 없음')} ({doc.to_dict().get('course_name', '')})"
        for doc in classes_ref}

    if not classes_dict:
        st.warning("먼저 '수업 관리' 메뉴에서 수업을 추가해주세요.")
        return

    col1, col2 = st.columns(2)
    with col1:
        selected_class_id = st.selectbox("수업 반 선택",
                                         options=list(classes_dict.keys()),
                                         format_func=lambda x: classes_dict.get(
                                             x, "이름 없음"))
    with col2:
        selected_date = st.date_input("날짜 선택", datetime.now())

    date_str = selected_date.strftime("%Y-%m-%d")

    if selected_class_id:
        if st.button("➕ 진도 추가", type="primary"):
            progress_dialog(class_id=selected_class_id, date_str=date_str)

        st.subheader(f"'{date_str}'의 진도 기록")
        progress_ref = db.collection("classes").document(
            selected_class_id).collection("progress").where("date", "==",
                                                            date_str).order_by(
            "period").stream()
        progress_list = list(progress_ref)

        if not progress_list:
            st.info("해당 날짜에 등록된 진도 기록이 없습니다.")
        else:
            for progress in progress_list:
                p = progress.to_dict()
                with st.container(border=True):
                    st.markdown(f"**{p.get('period')}교시: {p.get('topic')}**")
                    if p.get('notes'):
                        st.text(f"특기사항: {p.get('notes')}")

                    b_col1, b_col2, _ = st.columns([1, 1, 8])
                    if b_col1.button("수정", key=f"edit_progress_{progress.id}",
                                     use_container_width=True):
                        progress_dialog(selected_class_id, date_str,
                                        progress_id=progress.id)
                    if b_col2.button("삭제", key=f"delete_progress_{progress.id}",
                                     type="secondary",
                                     use_container_width=True):
                        db.collection("classes").document(
                            selected_class_id).collection("progress").document(
                            progress.id).delete()
                        st.success("진도 기록이 삭제되었습니다.")
                        st.rerun()


# 3.5. 출결 관리
def attendance_management():
    st.header("📋 출결 관리")
    st.markdown("학생별 출결 상태 및 특기사항을 관리합니다.")

    classes_ref = db.collection("classes").stream()
    classes_dict = {
        doc.id: f"{doc.to_dict().get('class_name', '이름 없음')} ({doc.to_dict().get('course_name', '')})"
        for doc in classes_ref}

    if not classes_dict:
        st.warning("먼저 '수업 관리' 메뉴에서 수업을 추가해주세요.")
        return

    col1, col2 = st.columns(2)
    with col1:
        selected_class_id = st.selectbox("수업 반 선택",
                                         options=list(classes_dict.keys()),
                                         format_func=lambda x: classes_dict.get(
                                             x, "이름 없음"))
    with col2:
        selected_date = st.date_input("날짜 선택", datetime.now())

    date_str = selected_date.strftime("%Y-%m-%d")

    if selected_class_id:
        students_ref = db.collection("classes").document(
            selected_class_id).collection("students").order_by(
            "student_number").stream()
        students_list = list(students_ref)

        if not students_list:
            st.info("이 반에 등록된 학생이 없습니다. '학생 관리' 메뉴에서 추가해주세요.")
            return

        attendance_ref = db.collection("attendance").where("class_id", "==",
                                                           selected_class_id).where(
            "date", "==", date_str).stream()
        attendance_data = {doc.to_dict().get('student_id'): doc.to_dict() for
                           doc in attendance_ref}

        st.subheader(f"'{date_str}' 출결 현황")

        with st.form("attendance_form"):
            attendance_inputs = {}

            header_cols = st.columns([2, 3, 3, 5])
            header_cols[0].markdown("**학번**")
            header_cols[1].markdown("**이름**")
            header_cols[2].markdown("**출결 상태**")
            header_cols[3].markdown("**특기사항**")

            for student in students_list:
                s_id = student.id
                s_data = student.to_dict()

                existing_att = attendance_data.get(s_id, {})

                cols = st.columns([2, 3, 3, 5])
                cols[0].text(s_data.get("student_number"))
                cols[1].text(s_data.get("name"))

                status = cols[2].selectbox(
                    "출결 상태",
                    ["출석", "결석", "지각", "공결"],
                    index=["출석", "결석", "지각", "공결"].index(
                        existing_att.get("status", "출석")),
                    key=f"status_{s_id}",
                    label_visibility="collapsed"
                )
                notes = cols[3].text_input(
                    "특기사항",
                    value=existing_att.get("notes", ""),
                    key=f"notes_{s_id}",
                    label_visibility="collapsed"
                )

                attendance_inputs[s_id] = {
                    "student_data": s_data,
                    "status": status,
                    "notes": notes
                }

            submitted = st.form_submit_button("💾 일괄 저장",
                                              use_container_width=True,
                                              type="primary")
            if submitted:
                with st.spinner("출결 정보를 저장 중입니다..."):
                    batch = db.batch()
                    attendance_collection = db.collection("attendance")

                    for s_id, inputs in attendance_inputs.items():
                        query = attendance_collection.where("class_id", "==",
                                                            selected_class_id).where(
                            "date", "==", date_str).where("student_id", "==",
                                                          s_id).limit(
                            1).stream()
                        existing_docs = list(query)

                        data = {
                            "class_id": selected_class_id,
                            "student_id": s_id,
                            "student_number": inputs["student_data"].get(
                                "student_number"),
                            "student_name": inputs["student_data"].get("name"),
                            "date": date_str,
                            "status": inputs["status"],
                            "notes": inputs["notes"],
                            "last_updated_at": firestore.SERVER_TIMESTAMP
                        }

                        if existing_docs:
                            doc_ref = existing_docs[0].reference
                            batch.update(doc_ref, data)
                        else:
                            doc_ref = attendance_collection.document()
                            batch.set(doc_ref, data)

                    batch.commit()
                st.success("출결 정보가 성공적으로 저장되었습니다.")
                st.rerun()


# 3.6. 데이터 백업
def data_backup():
    st.header("💾 데이터 백업")
    st.markdown("Firestore의 데이터를 Google 스프레드시트로 내보냅니다.")

    with st.expander("ℹ️ 사전 설정 방법 안내"):
        st.markdown("""
        1.  Google Cloud Console에서 **Google Drive API**와 **Google Sheets API**를 활성화합니다.
        2.  서비스 계정을 생성하고 **편집자** 역할을 부여한 뒤, JSON 키를 다운로드합니다.
        3.  다운로드한 JSON 키의 내용을 복사하여 Streamlit Cloud의 `GSPREAD_KEY` Secrets에 붙여넣습니다.
        4.  백업할 Google 스프레드시트를 만들고, **공유** 버튼을 눌러 서비스 계정의 이메일 주소(`client_email`)를 추가하고 **편집자** 권한을 부여합니다.
        5.  스프레드시트의 URL에서 ID를 복사하여 아래 입력창에 붙여넣습니다.
            - 예: `https://docs.google.com/spreadsheets/d/`**`여기가 ID 부분`**`/edit`
        """)

    spreadsheet_id = st.text_input("Google 스프레드시트 ID",
                                   placeholder="여기에 스프레드시트 ID를 붙여넣으세요.")

    if st.button("📤 스프레드시트로 내보내기", type="primary", disabled=not spreadsheet_id):
        with st.spinner("데이터를 내보내는 중입니다. 잠시만 기다려주세요..."):
            try:
                spreadsheet = gc.open_by_key(spreadsheet_id)

                collections_to_backup = ["courses", "classes", "attendance"]

                for collection_name in collections_to_backup:
                    docs = db.collection(collection_name).stream()
                    data = [doc.to_dict() for doc in docs]
                    if not data:
                        st.info(f"'{collection_name}' 컬렉션에 데이터가 없어 건너뜁니다.")
                        continue

                    df = pd.DataFrame(data)
                    for col in df.columns:
                        if pd.api.types.is_datetime64_any_dtype(df[col]):
                            df[col] = df[col].astype(str)

                    try:
                        worksheet = spreadsheet.worksheet(collection_name)
                        worksheet.clear()
                    except gspread.WorksheetNotFound:
                        worksheet = spreadsheet.add_worksheet(
                            title=collection_name, rows=100, cols=20)

                    set_with_dataframe(worksheet, df)
                    st.write(f"✅ '{collection_name}' 컬렉션 백업 완료.")

                all_classes = list(db.collection("classes").stream())

                all_students = []
                for class_doc in all_classes:
                    class_data = class_doc.to_dict()
                    students = db.collection("classes").document(
                        class_doc.id).collection("students").stream()
                    for student in students:
                        student_data = student.to_dict()
                        student_data['class_id'] = class_doc.id
                        student_data['class_name'] = class_data.get(
                            'class_name')
                        all_students.append(student_data)

                if all_students:
                    df_students = pd.DataFrame(all_students)
                    for col in df_students.columns:
                        if pd.api.types.is_datetime64_any_dtype(
                                df_students[col]):
                            df_students[col] = df_students[col].astype(str)
                    try:
                        worksheet = spreadsheet.worksheet("students")
                        worksheet.clear()
                    except gspread.WorksheetNotFound:
                        worksheet = spreadsheet.add_worksheet(title="students",
                                                              rows=100, cols=20)
                    set_with_dataframe(worksheet, df_students)
                    st.write("✅ 'students' 컬렉션 백업 완료.")
                else:
                    st.info("'students' 컬렉션에 데이터가 없어 건너뜁니다.")

                all_progress = []
                for class_doc in all_classes:
                    class_data = class_doc.to_dict()
                    progress_items = db.collection("classes").document(
                        class_doc.id).collection("progress").stream()
                    for item in progress_items:
                        item_data = item.to_dict()
                        item_data['class_id'] = class_doc.id
                        item_data['class_name'] = class_data.get('class_name')
                        all_progress.append(item_data)

                if all_progress:
                    df_progress = pd.DataFrame(all_progress)
                    for col in df_progress.columns:
                        if pd.api.types.is_datetime64_any_dtype(
                                df_progress[col]):
                            df_progress[col] = df_progress[col].astype(str)
                    try:
                        worksheet = spreadsheet.worksheet("progress")
                        worksheet.clear()
                    except gspread.WorksheetNotFound:
                        worksheet = spreadsheet.add_worksheet(title="progress",
                                                              rows=100, cols=20)
                    set_with_dataframe(worksheet, df_progress)
                    st.write("✅ 'progress' 컬렉션 백업 완료.")
                else:
                    st.info("'progress' 컬렉션에 데이터가 없어 건너뜁니다.")

                st.success("모든 데이터 백업이 완료되었습니다!")

            except gspread.exceptions.SpreadsheetNotFound:
                st.error("스프레드시트를 찾을 수 없습니다. ID를 확인하거나 서비스 계정에 공유했는지 확인하세요.")
            except Exception as e:
                st.error(f"데이터 내보내기 중 오류 발생: {e}")


# --- 4. 메인 애플리케이션 로직 ---

def main():
    st.title("👨‍🏫 교사용 수업 관리 시스템")

    with st.sidebar:
        st.image(
            "https://www.gstatic.com/images/branding/product/1x/drive_2020q4_48dp.png",
            width=60)
        st.header("메뉴")
        menu_options = ["교과 관리", "수업 관리", "학생 관리", "진도 관리", "출결 관리", "데이터 백업"]
        selected_menu = st.selectbox("이동할 메뉴를 선택하세요", menu_options)

    if selected_menu == "교과 관리":
        course_management()
    elif selected_menu == "수업 관리":
        class_management()
    elif selected_menu == "학생 관리":
        student_management()
    elif selected_menu == "진도 관리":
        progress_management()
    elif selected_menu == "출결 관리":
        attendance_management()
    elif selected_menu == "데이터 백업":
        data_backup()


if __name__ == "__main__":
    main()
