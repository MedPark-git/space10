# 재고현황 및 재고분석

MedPark 사내용 재고관리 웹 애플리케이션입니다. Flask/Gunicorn/PostgreSQL/SQLAlchemy로 구성되며 운영 환경에서 SQLite로 대체하지 않습니다.

## 주요 메뉴

- 대시보드: 총 품목, 재고금액, 안전재고 미달, 최근 입출고
- 품목 관리: 품목 등록·수정, 현재고·안전재고·단가 관리
- 입출고 관리: 입고·출고 등록 및 이력 검색
- 재고 분석: 재고금액 상위 품목, 안전재고 미달, 장기 미동 품목
- 사용자 관리, 내 정보, 비밀번호 변경, 감사 로그

## 운영 설정

AI SPACE가 제공하는 `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`가 모두 필요합니다. `SECRET_KEY`와 최초 관리자용 환경변수는 AI SPACE에서 별도로 설정합니다. 실제 비밀번호나 DB 접속정보는 저장소에 기록하지 않습니다.

시작 명령:

```text
gunicorn --workers 2 --threads 4 --timeout 120 --bind 0.0.0.0:${PORT:-8000} app:app
```

서버 시작 시 PostgreSQL advisory lock으로 초기 스키마 처리를 직렬화합니다. 사용자 테이블이 비어 있을 때만 부트스트랩 관리자를 생성합니다.

