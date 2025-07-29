import streamlit as st
import firebase_admin
from firebase_admin import credentials, firestore, storage, auth
import pandas as pd
import datetime
import tempfile
import os, json




# Firebase 초기화
if not firebase_admin._apps:
    firebase_key = json.loads(os.environ["FIREBASE_KEY"])
    cred = credentials.Certificate(firebase_key)
    firebase_admin.initialize_app(cred, {
        'storageBucket': 'class-recorder-6ce3f.firebasestorage.app'
    })

db = firestore.client()
bucket = storage.bucket()

st.set_page_config(page_title="Course Management App", layout="wide")

# if "user" not in st.session_state:
#     st.session_state.user = None
#
# def login():
#     st.title("로그인")
#     email = st.text_input("이메일")
#     password = st.text_input("비밀번호", type="password")
#     if st.button("로그인"):
#         try:
#             user = auth.get_user_by_email(email)
#             st.session_state.user = user
#             st.success("로그인 성공!")
#         except:
#             st.error("로그인 실패")

def manage_courses():
    st.header("담당 교과 관리")
    courses_ref = db.collection("courses")
    courses = courses_ref.stream()
    data = [[c.to_dict().get("name"), c.to_dict().get("year"), c.to_dict().get("semester"), c.to_dict().get("file_url")] for c in courses]
    st.dataframe(pd.DataFrame(data, columns=["교과명", "학년도", "학기", "계획서 URL"]))

    st.subheader("교과 추가")
    name = st.text_input("교과명")
    year = st.selectbox("학년도", list(range(2020, 2031)))
    semester = st.selectbox("학기", [1, 2])
    file = st.file_uploader("PDF 업로드", type=["pdf"])

    if st.button("저장"):
        if file and file.size <= 10*1024*1024:
            tmp = tempfile.NamedTemporaryFile(delete=False)
            tmp.write(file.read())
            tmp.close()
            blob = bucket.blob(f"courses/{file.name}")
            blob.upload_from_filename(tmp.name)
            url = blob.generate_signed_url(datetime.timedelta(days=365))
            courses_ref.add({"name": name, "year": year, "semester": semester, "file_url": url})
            st.success("교과가 추가되었습니다.")
        else:
            st.error("10MB 이하의 PDF 파일만 업로드 가능합니다.")

def manage_classes():
    st.header("수업 등록 및 관리")
    classes_ref = db.collection("classes")
    classes = classes_ref.stream()
    data = [[c.to_dict().get("year"), c.to_dict().get("semester"), c.to_dict().get("course"), c.to_dict().get("class_name"), c.to_dict().get("days"), c.to_dict().get("period") ] for c in classes]
    st.dataframe(pd.DataFrame(data, columns=["학년도", "학기", "교과", "학반", "요일", "교시"]))

    st.subheader("수업 추가")
    year = st.selectbox("학년도", list(range(2020, 2031)), key="class_year")
    semester = st.selectbox("학기", [1, 2], key="class_sem")
    course_list = [c.to_dict().get("name") for c in db.collection("courses").stream()]
    course = st.selectbox("교과 선택", course_list)
    class_name = st.text_input("학반")
    days = st.multiselect("요일 선택", ["월", "화", "수", "목", "금"])
    period = st.number_input("교시", min_value=1, max_value=10, step=1)

    if st.button("수업 저장"):
        classes_ref.add({"year": year, "semester": semester, "course": course, "class_name": class_name, "days": days, "period": period})
        st.success("수업이 추가되었습니다.")

def manage_students():
    st.header("학생 등록 및 관리")
    class_list = [c.id for c in db.collection("classes").stream()]
    class_id = st.selectbox("수업 반 선택", class_list)
    students_ref = db.collection("classes").document(class_id).collection("students")
    students = students_ref.stream()
    data = [[s.to_dict().get("id"), s.to_dict().get("name")] for s in students]
    st.dataframe(pd.DataFrame(data, columns=["학번", "성명"]))

    st.subheader("학생 추가")
    sid = st.text_input("학번")
    sname = st.text_input("성명")
    if st.button("학생 추가"):
        students_ref.add({"id": sid, "name": sname})
        st.success("학생이 추가되었습니다.")

    st.subheader("CSV 업로드")
    file = st.file_uploader("CSV 업로드", type=["csv"])
    if file and st.button("CSV로 등록"):
        df = pd.read_csv(file)
        for _, row in df.iterrows():
            students_ref.add({"id": row["학번"], "name": row["성명"]})
        st.success("CSV 학생 등록 완료")

def manage_progress():
    st.header("진도 및 특기사항 기록")
    class_list = [c.id for c in db.collection("classes").stream()]
    class_id = st.selectbox("수업 반 선택", class_list)
    date = st.date_input("날짜 선택")
    period = st.number_input("교시", min_value=1, max_value=10, step=1, key="progress_period")
    content = st.text_area("진도 내용")
    notes = st.text_area("특기사항")

    if st.button("기록 저장"):
        db.collection("classes").document(class_id).collection("progress").add({
            "date": str(date), "period": period, "content": content, "notes": notes
        })
        st.success("진도 기록 저장 완료")

def manage_attendance():
    st.header("출결 및 특기사항 기록")
    class_list = [c.id for c in db.collection("classes").stream()]
    class_id = st.selectbox("수업 반 선택", class_list)
    date = st.date_input("날짜 선택", key="att_date")

    students = db.collection("classes").document(class_id).collection("students").stream()
    records = []
    for s in students:
        st.subheader(f"{s.to_dict().get('name')} ({s.to_dict().get('id')})")
        status = st.selectbox("출결 상태", ["출석", "지각", "결석", "조퇴"], key=f"status_{s.id}")
        note = st.text_input("특기사항", key=f"note_{s.id}")
        records.append({"id": s.id, "name": s.to_dict().get("name"), "status": status, "note": note})

    if st.button("출결 저장"):
        for r in records:
            db.collection("classes").document(class_id).collection("attendance").add({
                "date": str(date), "student_id": r["id"], "name": r["name"], "status": r["status"], "note": r["note"]
            })
        st.success("출결 기록 저장 완료")


menu = st.sidebar.selectbox("메뉴", [
    "담당 교과 관리",
    "수업 등록 및 관리",
    "학생 등록 및 관리",
    "진도 및 특기사항 기록",
    "출결 및 특기사항 기록"
])

if menu == "담당 교과 관리":
    manage_courses()
elif menu == "수업 등록 및 관리":
    manage_classes()
elif menu == "학생 등록 및 관리":
    manage_students()
elif menu == "진도 및 특기사항 기록":
    manage_progress()
elif menu == "출결 및 특기사항 기록":
    manage_attendance()
