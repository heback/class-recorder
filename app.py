import streamlit as st
import firebase_admin
from firebase_admin import credentials, firestore, storage
import pandas as pd
import datetime

# Firebase 초기화
if not firebase_admin._apps:
    FIREBASE_KEY = dict(st.secrets["FIREBASE_KEY"])
    cred = credentials.Certificate(FIREBASE_KEY)
    firebase_admin.initialize_app(cred, {
        'storageBucket': FIREBASE_KEY["storageBucket"]
    })

db = firestore.client()
bucket = storage.bucket()

# 메뉴
menu = st.sidebar.selectbox("메뉴 선택", ["교과 관리", "수업 관리", "학생 관리", "진도 관리", "출결 관리"])

### 교과 관리 ###
if menu == "교과 관리":
    st.header("교과 관리")
    subjects_ref = db.collection("subjects")

    # 교과 목록 표시
    subjects = subjects_ref.stream()
    data = [{"id": s.id, **s.to_dict()} for s in subjects]
    st.table(pd.DataFrame(data))

    st.subheader("교과 추가")
    with st.form("add_subject"):
        name = st.text_input("교과명")
        year = st.number_input("학년도", 2020, 2100, 2025)
        semester = st.selectbox("학기", [1, 2])
        file = st.file_uploader("계획서 업로드 (PDF, 10MB 제한)", type=["pdf"])
        submitted = st.form_submit_button("저장")

        if submitted:
            if file and file.size <= 10*1024*1024:
                blob = bucket.blob(f"plans/{file.name}")
                blob.upload_from_file(file, content_type="application/pdf")
                plan_url = blob.public_url
                subjects_ref.add({"name": name, "year": year, "semester": semester, "plan_url": plan_url})
                st.success("교과가 추가되었습니다.")
            else:
                st.error("파일은 PDF 형식이며 10MB 이하이어야 합니다.")

### 수업 관리 ###
if menu == "수업 관리":
    st.header("수업 관리")
    classes_ref = db.collection("classes")
    subjects_ref = db.collection("subjects")
    subjects = subjects_ref.stream()
    subject_list = {s.id: s.to_dict()["name"] for s in subjects}

    classes = classes_ref.stream()
    data = [{"id": c.id, **c.to_dict()} for c in classes]
    st.table(pd.DataFrame(data))

    with st.form("add_class"):
        year = st.number_input("학년도", 2020, 2100, 2025)
        semester = st.selectbox("학기", [1, 2])
        subject_id = st.selectbox("교과 선택", options=list(subject_list.keys()), format_func=lambda x: subject_list[x])
        class_name = st.text_input("반 이름")
        schedule_day = st.selectbox("요일", ["월", "화", "수", "목", "금"])
        schedule_period = st.number_input("교시", 1, 10, 1)
        submitted = st.form_submit_button("저장")

        if submitted:
            classes_ref.add({
                "subject_id": subject_id,
                "year": year,
                "semester": semester,
                "class_name": class_name,
                "schedule": [{"day": schedule_day, "period": schedule_period}]
            })
            st.success("수업이 추가되었습니다.")

### 학생 관리 ###
if menu == "학생 관리":
    st.header("학생 관리")
    classes_ref = db.collection("classes")
    students_ref = db.collection("students")

    classes = classes_ref.stream()
    class_list = {c.id: c.to_dict()["class_name"] for c in classes}

    class_id = st.selectbox("반 선택", options=list(class_list.keys()), format_func=lambda x: class_list[x])
    students = students_ref.where("class_id", "==", class_id).stream()
    data = [{"id": s.id, **s.to_dict()} for s in students]
    st.table(pd.DataFrame(data))

    st.subheader("학생 추가")
    with st.form("add_student"):
        student_no = st.text_input("학번")
        name = st.text_input("성명")
        submitted = st.form_submit_button("추가")

        if submitted:
            students_ref.add({"class_id": class_id, "student_no": student_no, "name": name})
            st.success("학생이 추가되었습니다.")

    st.subheader("CSV 업로드")
    csv_file = st.file_uploader("CSV 파일 업로드", type=["csv"])
    if csv_file:
        df = pd.read_csv(csv_file)
        for _, row in df.iterrows():
            students_ref.add({"class_id": class_id, "student_no": row["학번"], "name": row["성명"]})
        st.success("학생 목록이 업로드되었습니다.")

### 진도 관리 ###
if menu == "진도 관리":
    st.header("진도 관리")
    classes_ref = db.collection("classes")
    class_list = {c.id: c.to_dict()["class_name"] for c in classes_ref.stream()}

    class_id = st.selectbox("반 선택", options=list(class_list.keys()), format_func=lambda x: class_list[x])
    date = st.date_input("날짜", datetime.date.today())
    period = st.number_input("교시", 1, 10, 1)
    content = st.text_area("진도 내용")
    notes = st.text_area("특기사항")

    if st.button("저장"):
        db.collection("progress").add({
            "class_id": class_id,
            "date": date.isoformat(),
            "period": period,
            "content": content,
            "notes": notes
        })
        st.success("진도가 저장되었습니다.")

### 출결 관리 ###
if menu == "출결 관리":
    st.header("출결 관리")
    classes_ref = db.collection("classes")
    students_ref = db.collection("students")

    class_list = {c.id: c.to_dict()["class_name"] for c in classes_ref.stream()}
    class_id = st.selectbox("반 선택", options=list(class_list.keys()), format_func=lambda x: class_list[x])

    date = st.date_input("날짜", datetime.date.today())

    students = students_ref.where("class_id", "==", class_id).stream()
    for s in students:
        st.write(f"학생: {s.to_dict()['name']} ({s.to_dict()['student_no']})")
        status = st.selectbox("출결 상태", ["출석", "결석", "지각", "조퇴"], key=s.id)
        notes = st.text_input("특기사항", key=s.id+"_note")
        if st.button("저장", key=s.id+"_btn"):
            db.collection("attendance").add({
                "class_id": class_id,
                "student_id": s.id,
                "date": date.isoformat(),
                "status": status,
                "notes": notes
            })
            st.success("저장되었습니다.")
