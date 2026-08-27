"""YouTube 아웃라이어 검색 - 웹 화면 버전.

터미널 명령어 대신 브라우저에서 검색어를 입력하고 버튼을 눌러 쓰는 화면.
로직은 youtube_outlier.py의 함수를 그대로 재사용한다.
"""

import html
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests
import streamlit as st

from youtube_outlier import (
    ApiError,
    DAILY_QUOTA,
    HISTORY_MAX_DAYS,
    HISTORY_MAX_ENTRIES,
    QuotaTracker,
    YouTubeClient,
    SEARCH_HARD_CAP,
    assign_ranks_and_order,
    build_rows,
    clear_history,
    load_history,
    load_usage,
    record_usage,
    save_search_to_history,
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
    """CSV 다운로드용. rows는 실검색 결과(published_at이 datetime)도,
    기록에서 불러온 결과(published_at이 문자열)도 둘 다 받을 수 있다."""
    data = []
    for r in rows:
        data.append(
            {
                "순위": r["rank"],
                "제목": r["title"],
                "채널": r["channel_title"],
                "구독자수": r["subscriber_count"],
                "조회수": r["view_count"],
                "점수": round(r["score"], 2) if r["score"] is not None else None,
                "게시일": _format_date(r["published_at"]),
                "video_id": r["video_id"],
                "링크": r["url"],
            }
        )
    return pd.DataFrame(data)


def _format_date(published_at):
    """실검색 결과(datetime)와 기록에서 불러온 결과(ISO 문자열) 둘 다 'YYYY-MM-DD'로 통일."""
    if hasattr(published_at, "strftime"):
        return published_at.strftime("%Y-%m-%d")
    return str(published_at)[:10]


CARD_CSS = """
<style>
.yt-card-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
    gap: 12px;
    margin: 8px 0 16px 0;
}
.yt-card {
    display: block; text-decoration: none; color: inherit;
    border: 1px solid rgba(128,128,128,0.3); border-radius: 10px;
    overflow: hidden; background: rgba(128,128,128,0.04);
}
.yt-card:hover { border-color: rgba(128,128,128,0.6); }
.yt-card-thumb { width: 100%; aspect-ratio: 16/9; object-fit: cover; display: block; }
.yt-card-body { padding: 10px 12px; }
.yt-card-rank { font-size: 0.75rem; opacity: 0.6; font-weight: 700; }
.yt-card-title {
    font-weight: 600; font-size: 0.95rem; line-height: 1.3; margin: 2px 0 4px 0;
    display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
}
.yt-card-channel { font-size: 0.8rem; opacity: 0.7; margin-bottom: 6px; }
.yt-card-stats { display: flex; gap: 10px; flex-wrap: wrap; font-size: 0.82rem; margin-bottom: 4px; }
.yt-card-score { font-weight: 700; }
.yt-card-date { font-size: 0.75rem; opacity: 0.55; }
</style>
"""


def render_result_cards(rows):
    """검색 결과를 폰/PC 모두에서 보기 편한 카드 그리드로 렌더링한다."""
    cards = []
    for r in rows:
        title = html.escape(r["title"])
        channel = html.escape(r["channel_title"])
        subs = "N/A" if r["subscriber_count"] is None else f"{r['subscriber_count']:,}"
        score = "N/A" if r["score"] is None else f"{r['score']:.2f}"
        rank = f"#{r['rank']}" if isinstance(r["rank"], int) else "-"
        thumb = f"https://i.ytimg.com/vi/{r['video_id']}/mqdefault.jpg"
        cards.append(
            f"""<a class="yt-card" href="{r['url']}" target="_blank" rel="noopener">
<img class="yt-card-thumb" src="{thumb}" loading="lazy" alt="">
<div class="yt-card-body">
<div class="yt-card-rank">{rank}</div>
<div class="yt-card-title">{title}</div>
<div class="yt-card-channel">{channel}</div>
<div class="yt-card-stats"><span>👁 {r['view_count']:,}</span><span>👤 {subs}</span>
<span class="yt-card-score">⭐ {score}</span></div>
<div class="yt-card-date">{_format_date(r['published_at'])}</div>
</div></a>"""
        )
    st.markdown(f'<div class="yt-card-grid">{"".join(cards)}</div>', unsafe_allow_html=True)


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
st.markdown(CARD_CSS, unsafe_allow_html=True)
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

tab_search, tab_history = st.tabs(["🔍 검색", "🗂 기록"])

with tab_search:
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

            options = {
                "order": ORDER_LABELS[order_label],
                "sort_by": SORT_LABELS[sort_label],
                "max_results": max_results,
                "days": days,
                "min_views": min_views,
                "score_mode": SCORE_MODE_LABELS[score_mode_label],
            }

            quota = QuotaTracker()
            with st.spinner("검색 중입니다... (영상 수가 많으면 몇 초 더 걸려요)"):
                try:
                    rows = run_search(
                        quota,
                        keyword=keyword,
                        api_key=api_key,
                        order=options["order"],
                        max_results=max_results,
                        days=days,
                        min_views=min_views,
                        score_mode=options["score_mode"],
                        sort_by=options["sort_by"],
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
                save_search_to_history(keyword, options, rows, quota.total_units)

                if not rows:
                    st.warning("조건에 맞는 영상이 없습니다. 필터를 조금 완화해보세요.")
                else:
                    st.success(f"{len(rows)}개 영상을 찾았습니다.")
                    render_result_cards(rows)
                    csv_bytes = (
                        rows_to_dataframe(rows).to_csv(index=False).encode("utf-8-sig")
                    )
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

with tab_history:
    st.caption(f"최근 {HISTORY_MAX_DAYS}일 · 최대 {HISTORY_MAX_ENTRIES}건까지 자동 보관됩니다.")
    history = load_history()

    if not history:
        st.info("아직 검색 기록이 없습니다.")
    else:
        col1, col2 = st.columns([3, 1])
        with col1:
            filter_kw = st.text_input("검색어로 필터링", key="history_filter", placeholder="예: 브이로그")
        with col2:
            if st.button("🗑 전체 기록 삭제", use_container_width=True):
                clear_history()
                st.rerun()

        filtered = (
            [e for e in history if filter_kw.strip() in e["keyword"]]
            if filter_kw.strip()
            else history
        )

        if not filtered:
            st.warning("필터 조건에 맞는 기록이 없습니다.")
        else:
            default_n = min(10, len(filtered))
            show_n = (
                st.slider("표시할 기록 개수", 1, len(filtered), default_n)
                if len(filtered) > 1
                else len(filtered)
            )
            for idx, entry in enumerate(filtered[:show_n]):
                ts_display = entry["timestamp"][:16].replace("T", " ")
                opts = entry.get("options", {})
                label = f"{ts_display} · '{entry['keyword']}' · {entry['result_count']}개 결과"
                with st.expander(label):
                    st.caption(
                        f"가져오기 기준: {opts.get('order', '-')} · 정렬: {opts.get('sort_by', '-')} · "
                        f"검색개수: {opts.get('max_results', '-')} · 최근 {opts.get('days', 0)}일 · "
                        f"최소조회수: {opts.get('min_views', 0):,} · 점수방식: {opts.get('score_mode', '-')} · "
                        f"소모 유닛: {entry.get('quota_used', 0)}"
                    )
                    if entry["rows"]:
                        render_result_cards(entry["rows"])
                        csv_bytes = (
                            rows_to_dataframe(entry["rows"]).to_csv(index=False).encode("utf-8-sig")
                        )
                        st.download_button(
                            "📥 CSV로 저장",
                            data=csv_bytes,
                            file_name=f"{entry['keyword']}_기록.csv",
                            mime="text/csv",
                            key=f"hist_csv_{idx}_{entry['timestamp']}",
                        )
                    else:
                        st.write("결과 없음")
