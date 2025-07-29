import streamlit as st
import firebase_admin
from firebase_admin import credentials, firestore, storage
import pandas as pd
import datetime
import io

# 서비스 계정 키를 st.secrets에 넣는 방법:
# .streamlit/secrets.toml 파일에 아래처럼 추가
#
# [firebase]
# type = "service_account"
# project_id = "프로젝트ID"
# private_key_id = "키ID"
# private_key = """-----BEGIN PRIVATE KEY-----\n줄바꿈은 반드시 \n으로\n표시해야 합니다\n-----END PRIVATE KEY-----\n"""
# client_email = "firebase-adminsdk@프로젝트ID.iam.gserviceaccount.com"
# client_id = "클라이언트ID"
# auth_uri = "https://accounts.google.com/o/oauth2/auth"
# token_uri = "https://oauth2.googleapis.com/token"
# auth_provider_x509_cert_url = "https://www.googleapis.com/oauth2/v1/certs"
# client_x509_cert_url = "..."
#
# private_key 값에 실제 줄바꿈을 넣지 말고, 모든 줄바꿈을 \n 문자열로 치환해야 함

# Firebase 연결 테스트 및 초기화
def init_firebase():
    try:
        if not firebase_admin._apps:
            # st.secrets["firebase"]를 dict로 변환하고 private_key의 \n 처리
            firebase_config = dict(st.secrets["FIREBASE_KEY"])
            if "private_key" in firebase_config:
                firebase_config["private_key"] = firebase_config["private_key"].replace("\\n", "\n")
            cred = credentials.Certificate(firebase_config)
            firebase_admin.initialize_app(cred, {
                'storageBucket': st.secrets["storageBucket"]
            })
        st.success("Firebase 초기화 성공")
        return firestore.client(), storage.bucket()
    except Exception as e:
        st.error(f"Firebase 초기화 실패: {e}")
        return None, None

db, bucket = init_firebase()
if db is None:
    st.stop()

# Firestore 연결 테스트
def test_firestore_connection():
    try:
        list(db.collections())
        st.success("Firestore 연결 성공")
    except Exception as e:
        st.error(f"Firestore 연결 실패: {e}")

test_firestore_connection()

# 메뉴
menu = st.sidebar.selectbox("메뉴 선택", ["교과 관리", "수업 관리", "학생 관리", "진도 관리", "출결 관리"])

### 교과 관리 ###
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
                    subjects_ref.add({"name": name, "year": year, "semester": semester, "plan_url": plan_url})
                    st.success("교과가 추가되었습니다.")
                except Exception as e:
                    st.error(f"교과 추가 실패: {e}")
            else:
                st.error("파일은 PDF 형식이며 10MB 이하이어야 합니다.")

# 이후 수업 관리, 학생 관리, 진도 관리, 출결 관리 부분도 try/except로 Firestore 접근 시 오류를 표시하도록 동일한 방식으로 적용
