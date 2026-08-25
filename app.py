"""YouTube 아웃라이어 검색 - 웹 화면 버전.

터미널 명령어 대신 브라우저에서 검색어를 입력하고 버튼을 눌러 쓰는 화면.
로직은 youtube_outlier.py의 함수를 그대로 재사용한다.
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests
import streamlit as st

from youtube_outlier import (
    ApiError,
    DAILY_QUOTA,
    QuotaTracker,
    YouTubeClient,
    SEARCH_HARD_CAP,
    assign_ranks_and_order,
    build_rows,
    load_usage,
    record_usage,
)

CONFIG_PATH = Path(__file__).parent / "config.json"

ORDER_LABELS = {"조회수 높은 순 (추천)": "viewCount", "최신순": "date", "관련도순": "relevance"}
SORT_LABELS = {"점수 높은 순 (추천)": "score", "조회수 순": "views", "최신순": "date"}
SCORE_MODE_LABELS = {
    "기본 (조회수 ÷ 구독자수)": "simple",
    "최근 영상 가중치 (÷ 경과일수)": "recency",
}


def get_secret(name):
    """배포 환경(Streamlit Cloud)의 secrets에서 값을 읽는다. 로컬 실행 시엔 secrets가 없어 None."""
    try:
        return st.secrets.get(name)
    except Exception:
        return None


def load_saved_key():
    secret_key = get_secret("YOUTUBE_API_KEY")
    if secret_key:
        return secret_key
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text()).get("api_key", "")
        except Exception:
            return ""
    return ""


def save_key(key):
    CONFIG_PATH.write_text(json.dumps({"api_key": key}))


def run_search(quota, keyword, api_key, order, max_results, days, min_views, score_mode, sort_by):
    """quota는 호출한 쪽에서 만들어 넘긴다 - 도중에 에러가 나도 그때까지 쓴 유닛을 알 수 있게."""
    client = YouTubeClient(api_key, quota)

    published_after = None
    if days and days > 0:
        published_after = (
            (datetime.now(timezone.utc) - timedelta(days=days)).isoformat().replace("+00:00", "Z")
        )

    video_ids = client.search_video_ids(keyword, order, max_results, published_after)
    if not video_ids:
        return []

    video_details = client.get_videos_details(video_ids)
    channel_ids = sorted({v["channel_id"] for v in video_details.values()})
    channel_details = client.get_channels_details(channel_ids)

    rows = build_rows(video_details, channel_details, score_mode)
    if min_views:
        rows = [r for r in rows if r["view_count"] >= min_views]
    if not rows:
        return []

    return assign_ranks_and_order(rows, sort_by)


def rows_to_dataframe(rows):
    data = []
    for r in rows:
        data.append(
            {
                "순위": r["rank"],
                "썸네일": f"https://i.ytimg.com/vi/{r['video_id']}/mqdefault.jpg",
                "제목": r["title"],
                "채널": r["channel_title"],
                "구독자수": r["subscriber_count"],
                "조회수": r["view_count"],
                "점수": round(r["score"], 2) if r["score"] is not None else None,
                "게시일": r["published_at"].strftime("%Y-%m-%d"),
                "링크": r["url"],
            }
        )
    return pd.DataFrame(data)


def render_usage_badge():
    used = load_usage().get("units_used", 0)
    remaining = max(DAILY_QUOTA - used, 0)
    usage_badge.caption(
        f"📊 오늘 남은 예상 유닛: **{remaining:,}** / {DAILY_QUOTA:,} "
        "(PT 자정 리셋 · 이 프로그램 사용 기준 추정치)"
    )


st.set_page_config(page_title="유튜브 숨은 떡상 영상 찾기", page_icon="🔍", layout="wide")

APP_PASSWORD = get_secret("APP_PASSWORD")
if APP_PASSWORD:
    if "authenticated" not in st.session_state:
        st.session_state.authenticated = False
    if not st.session_state.authenticated:
        st.title("🔒 비밀번호 입력")
        pw = st.text_input("비밀번호", type="password")
        if st.button("입장"):
            if pw == APP_PASSWORD:
                st.session_state.authenticated = True
                st.rerun()
            else:
                st.error("비밀번호가 틀렸습니다.")
        st.stop()

st.title("🔍 유튜브 숨은 떡상 영상 찾기")
st.caption("구독자 수는 적은데 조회수는 높은 영상을 점수로 정렬해서 찾아줍니다.")
usage_badge = st.empty()
render_usage_badge()

with st.sidebar:
    st.header("⚙️ 설정")
    default_key = load_saved_key()
    api_key = st.text_input("YouTube API 키", value=default_key, type="password")
    save_checkbox = st.checkbox("이 컴퓨터에 키 저장하기", value=bool(default_key))

    st.divider()
    st.subheader("검색 옵션")
    order_label = st.selectbox("검색 후보를 가져올 기준", list(ORDER_LABELS.keys()))
    max_results = st.slider(
        "검색할 영상 개수", min_value=50, max_value=SEARCH_HARD_CAP, value=150, step=50
    )
    st.caption(f"예상 최대 소모: search.list {(-(-max_results // 50)) * 100} 유닛 (하루 10,000 유닛 중)")

    st.divider()
    st.subheader("필터")
    days = st.number_input("최근 며칠 이내 영상만 (0 = 제한 없음)", min_value=0, value=0)
    min_views = st.number_input("최소 조회수 (0 = 제한 없음)", min_value=0, value=0, step=1000)

    st.divider()
    st.subheader("점수 / 정렬")
    score_mode_label = st.radio("점수 계산 방식", list(SCORE_MODE_LABELS.keys()))
    sort_label = st.selectbox("결과 화면 정렬 기준", list(SORT_LABELS.keys()))

keyword = st.text_input("검색어", placeholder="예: 브이로그, 부업, 재테크...")
search_clicked = st.button("🔎 검색", type="primary", use_container_width=True)

if search_clicked:
    if not api_key:
        st.error("YouTube API 키를 먼저 입력해주세요. (왼쪽 사이드바)")
    elif not keyword:
        st.error("검색어를 입력해주세요.")
    else:
        if save_checkbox and api_key:
            save_key(api_key)

        quota = QuotaTracker()
        with st.spinner("검색 중입니다... (영상 수가 많으면 몇 초 더 걸려요)"):
            try:
                rows = run_search(
                    quota,
                    keyword=keyword,
                    api_key=api_key,
                    order=ORDER_LABELS[order_label],
                    max_results=max_results,
                    days=days,
                    min_views=min_views,
                    score_mode=SCORE_MODE_LABELS[score_mode_label],
                    sort_by=SORT_LABELS[sort_label],
                )
            except ApiError as e:
                st.error(f"YouTube API 오류: {e}")
                rows = None
            except requests.RequestException as e:
                st.error(f"네트워크 오류: {e}")
                rows = None
            finally:
                record_usage(quota.total_units)
                render_usage_badge()

        if rows is not None:
            if not rows:
                st.warning("조건에 맞는 영상이 없습니다. 필터를 조금 완화해보세요.")
            else:
                df = rows_to_dataframe(rows)
                st.success(f"{len(df)}개 영상을 찾았습니다.")
                st.dataframe(
                    df,
                    hide_index=True,
                    use_container_width=True,
                    column_config={
                        "썸네일": st.column_config.ImageColumn("썸네일"),
                        "구독자수": st.column_config.NumberColumn("구독자수", format="%d"),
                        "조회수": st.column_config.NumberColumn("조회수", format="%d"),
                        "점수": st.column_config.NumberColumn("점수", format="%.2f"),
                        "링크": st.column_config.LinkColumn("링크", display_text="▶ 보기"),
                    },
                )
                csv_bytes = df.drop(columns=["썸네일"]).to_csv(index=False).encode("utf-8-sig")
                st.download_button(
                    "📥 CSV로 저장",
                    data=csv_bytes,
                    file_name=f"{keyword}_결과.csv",
                    mime="text/csv",
                )

            if quota.total_units:
                st.divider()
                st.caption(
                    f"이번 검색 API 사용량: search.list {quota.search_calls}회, "
                    f"videos.list {quota.videos_calls}회, channels.list {quota.channels_calls}회 "
                    f"→ 총 {quota.total_units} / 10,000 유닛 (태평양시간 자정 리셋)"
                )
