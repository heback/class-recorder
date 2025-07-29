import streamlit as st
import firebase_admin
from firebase_admin import credentials, firestore, storage
import pandas as pd
import datetime
import io

def init_firebase():
    try:
        if not firebase_admin._apps:
            firebase_config = dict(st.secrets["FIREBASE_KEY"])
            # private_key의 줄바꿈 문자 처리
            if "private_key" in firebase_config:
                firebase_config["private_key"] = firebase_config["private_key"].replace("\\n", "\n")

            cred = credentials.Certificate(firebase_config)
            firebase_admin.initialize_app(cred, {
                "storageBucket": st.secrets["storageBucket"]
            })
        return firestore.client(), storage.bucket()
    except Exception as e:
        st.error(f"Firebase 초기화 실패: {e}")
        return None, None
st.write("1")
db, bucket = init_firebase()
if db is None:
    st.stop()
st.write("2")
# Firestore 연결 테스트
def test_firestore_connection():
    try:
        list(db.collections())
        st.success("Firestore 연결 성공")
    except Exception as e:
        st.error("Firestore 연결 실패. 서비스 계정 키나 권한을 확인하세요.")
        st.error(e)

test_firestore_connection()
st.write("3")
menu = st.sidebar.selectbox("메뉴 선택", ["교과 관리", "수업 관리", "학생 관리", "진도 관리", "출결 관리"])

if menu == "교과 관리" and db:
    st.header("교과 관리")
    try:
        subjects_ref = db.collection("subjects")
        subjects = subjects_ref.stream()
        data = [{"id": s.id, **s.to_dict()} for s in subjects]
        st.table(pd.DataFrame(data))
    except Exception as e:
        st.error(f"교과 목록 불러오기 실패: {e}")

    st.subheader("교과 추가")
    with st.form("add_subject"):
        name = st.text_input("교과명")
        year = st.number_input("학년도", 2020, 2100, 2025)
        semester = st.selectbox("학기", [1, 2])
        file = st.file_uploader("계획서 업로드 (PDF, 10MB 제한)", type=["pdf"])
        submitted = st.form_submit_button("저장")

        if submitted:
            if file and file.size <= 10*1024*1024:
                try:
                    blob = bucket.blob(f"plans/{file.name}")
                    blob.upload_from_file(file, content_type="application/pdf")
                    plan_url = blob.public_url
                    subjects_ref.add({
                        "name": name,
                        "year": year,
                        "semester": semester,
                        "plan_url": plan_url
                    })
                    st.success("교과가 추가되었습니다.")
                except Exception as e:
                    st.error(f"교과 추가 실패: {e}")
            else:
                st.error("파일은 PDF 형식이며 10MB 이하이어야 합니다.")
