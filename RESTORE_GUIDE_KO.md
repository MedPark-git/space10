# 복구 가이드

1. AI SPACE 프로젝트 백업과 같은 시점의 PostgreSQL 덤프를 준비합니다.
2. 새 PostgreSQL 인스턴스의 접속 환경변수가 자동 주입되었는지 확인합니다.
3. 애플리케이션을 배포한 뒤 데이터베이스 덤프를 복원합니다.
4. `/health`에서 `database`, `database_writable`, `application_ready`가 모두 `true`인지 확인합니다.
5. 관리자 로그인, 역할별 권한, 품목 조회, 최근 입출고 합계를 대조합니다.
6. `/app/user_data`에 영구 파일이 있다면 동일 경로로 복원합니다.

운영 DB를 SQLite로 변환하거나 실제 `.env`, 사용자 데이터, DB 덤프를 GitHub에 커밋하지 마십시오.

