#!/usr/bin/env python3
"""YouTube 아웃라이어(숨은 떡상 영상) 검색 CLI 도구.

키워드로 영상을 검색한 뒤, 구독자 수 대비 조회수가 높은 영상을 찾아
점수순으로 정렬해 보여준다. YouTube Data API v3의 일일 무료 할당량
(10,000 유닛)을 절약하기 위해 search.list(100유닛)는 최소한으로만
호출하고, 상세 정보는 videos.list/channels.list(각 1유닛)로 배치 조회한다.
"""

import argparse
import csv
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from tabulate import tabulate

API_BASE = "https://www.googleapis.com/youtube/v3"
SEARCH_COST = 100
DETAIL_COST = 1
PAGE_SIZE = 50
SEARCH_HARD_CAP = 500  # YouTube search.list가 사실상 허용하는 최대 접근량
DAILY_QUOTA = 10000
USAGE_PATH = Path(__file__).parent / "quota_usage.json"
HISTORY_PATH = Path(__file__).parent / "search_history.json"
HISTORY_MAX_ENTRIES = 200
HISTORY_MAX_DAYS = 90


def today_pacific_date():
    """유튜브 할당량이 리셋되는 태평양시간 기준 오늘 날짜."""
    return datetime.now(ZoneInfo("America/Los_Angeles")).date().isoformat()


def load_usage():
    """오늘(태평양시간) 누적 사용량을 로컬 파일에서 읽는다. 날짜가 바뀌었으면 0으로 리셋."""
    data = {}
    if USAGE_PATH.exists():
        try:
            data = json.loads(USAGE_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            data = {}
    if data.get("date") != today_pacific_date():
        data = {"date": today_pacific_date(), "units_used": 0}
    return data


def record_usage(units):
    """이번 실행에서 쓴 유닛을 오늘 누적치에 더해 저장하고, 누적치를 반환한다."""
    data = load_usage()
    data["units_used"] = data.get("units_used", 0) + units
    USAGE_PATH.write_text(json.dumps(data))
    return data["units_used"]


def _row_to_history_dict(r):
    """검색 결과 한 행을 JSON에 저장 가능한 형태로 변환 (datetime -> 문자열)."""
    return {
        "video_id": r["video_id"],
        "title": r["title"],
        "channel_title": r["channel_title"],
        "subscriber_count": r["subscriber_count"],
        "view_count": r["view_count"],
        "published_at": r["published_at"].isoformat(),
        "score": r["score"],
        "rank": r["rank"],
        "url": r["url"],
    }


def load_history():
    """저장된 검색 기록 전체를 최신순으로 반환한다."""
    if not HISTORY_PATH.exists():
        return []
    try:
        return json.loads(HISTORY_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return []


def _prune_history(history):
    """오래된 기록(HISTORY_MAX_DAYS 초과)을 지우고, 개수를 HISTORY_MAX_ENTRIES로 제한한다."""
    cutoff = datetime.now().astimezone() - timedelta(days=HISTORY_MAX_DAYS)
    kept = []
    for entry in history:
        try:
            ts = datetime.fromisoformat(entry["timestamp"])
        except (KeyError, ValueError):
            continue
        if ts >= cutoff:
            kept.append(entry)
    return kept[:HISTORY_MAX_ENTRIES]


def save_search_to_history(keyword, options, rows, quota_used):
    """이번 검색(검색어/옵션/결과/유닛)을 기록에 추가하고 오래된 기록은 정리한다."""
    history = load_history()
    entry = {
        "timestamp": datetime.now().astimezone().isoformat(),
        "keyword": keyword,
        "options": options,
        "result_count": len(rows),
        "quota_used": quota_used,
        "rows": [_row_to_history_dict(r) for r in rows],
    }
    history.insert(0, entry)
    history = _prune_history(history)
    HISTORY_PATH.write_text(json.dumps(history, ensure_ascii=False))
    return history


def clear_history():
    HISTORY_PATH.write_text("[]")


class QuotaTracker:
    def __init__(self):
        self.search_calls = 0
        self.videos_calls = 0
        self.channels_calls = 0

    @property
    def total_units(self):
        return (
            self.search_calls * SEARCH_COST
            + self.videos_calls * DETAIL_COST
            + self.channels_calls * DETAIL_COST
        )

    def report(self):
        lines = [
            "",
            "=== API 사용량 (이번 실행) ===",
            f"search.list   : {self.search_calls:>3}회 x {SEARCH_COST} = {self.search_calls * SEARCH_COST} 유닛",
            f"videos.list   : {self.videos_calls:>3}회 x {DETAIL_COST}   = {self.videos_calls * DETAIL_COST} 유닛",
            f"channels.list : {self.channels_calls:>3}회 x {DETAIL_COST}   = {self.channels_calls * DETAIL_COST} 유닛",
            f"합계          : {self.total_units} / 10,000 유닛 (태평양시간 자정 리셋)",
        ]
        print("\n".join(lines))


class ApiError(RuntimeError):
    pass


class YouTubeClient:
    def __init__(self, api_key, quota: QuotaTracker):
        self.api_key = api_key
        self.quota = quota
        self.session = requests.Session()

    def _get(self, path, params):
        params = dict(params)
        params["key"] = self.api_key
        resp = self.session.get(f"{API_BASE}/{path}", params=params, timeout=30)
        data = resp.json()
        if "error" in data:
            reason = data["error"].get("errors", [{}])[0].get("reason", "unknown")
            message = data["error"].get("message", "알 수 없는 오류")
            raise ApiError(f"YouTube API 오류 ({reason}): {message}")
        if not resp.ok:
            raise ApiError(f"HTTP {resp.status_code}: {resp.text}")
        return data

    def search_video_ids(self, keyword, order, max_results, published_after=None):
        """search.list로 영상 id 목록을 모은다. 호출당 100유닛."""
        video_ids = []
        page_token = None
        target = min(max_results, SEARCH_HARD_CAP)

        while len(video_ids) < target:
            params = {
                "part": "snippet",
                "q": keyword,
                "type": "video",
                "order": order,
                "maxResults": min(PAGE_SIZE, target - len(video_ids)),
            }
            if published_after:
                params["publishedAfter"] = published_after
            if page_token:
                params["pageToken"] = page_token

            data = self._get("search", params)
            self.quota.search_calls += 1

            ids = [item["id"]["videoId"] for item in data.get("items", []) if "videoId" in item.get("id", {})]
            video_ids.extend(ids)
            print(f"  search.list 호출 {self.quota.search_calls}회째... 누적 {len(video_ids)}개 후보 수집")

            page_token = data.get("nextPageToken")
            if not page_token or not ids:
                break

        return video_ids[:target]

    def get_videos_details(self, video_ids):
        """videos.list로 조회수/게시일/채널ID를 배치 조회. 50개당 1유닛."""
        results = {}
        for i in range(0, len(video_ids), PAGE_SIZE):
            batch = video_ids[i : i + PAGE_SIZE]
            data = self._get(
                "videos",
                {"part": "snippet,statistics", "id": ",".join(batch)},
            )
            self.quota.videos_calls += 1
            for item in data.get("items", []):
                results[item["id"]] = {
                    "title": item["snippet"]["title"],
                    "channel_id": item["snippet"]["channelId"],
                    "channel_title": item["snippet"]["channelTitle"],
                    "published_at": item["snippet"]["publishedAt"],
                    "view_count": int(item.get("statistics", {}).get("viewCount", 0)),
                }
        return results

    def get_channels_details(self, channel_ids):
        """channels.list로 구독자 수를 배치 조회. 50개당 1유닛."""
        results = {}
        for i in range(0, len(channel_ids), PAGE_SIZE):
            batch = channel_ids[i : i + PAGE_SIZE]
            data = self._get(
                "channels",
                {"part": "statistics", "id": ",".join(batch)},
            )
            self.quota.channels_calls += 1
            for item in data.get("items", []):
                stats = item.get("statistics", {})
                hidden = stats.get("hiddenSubscriberCount", False)
                sub_count = None if hidden else int(stats.get("subscriberCount", 0))
                results[item["id"]] = {"subscriber_count": sub_count, "hidden": hidden}
        return results


def parse_published_at(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def compute_score(view_count, subscriber_count, published_at, score_mode):
    if subscriber_count is None or subscriber_count <= 0:
        return None
    base = view_count / subscriber_count
    if score_mode == "recency":
        days_elapsed = max((datetime.now(timezone.utc) - published_at).days, 1)
        return base / days_elapsed
    return base


def build_rows(video_details, channel_details, score_mode):
    rows = []
    for video_id, v in video_details.items():
        ch = channel_details.get(v["channel_id"], {"subscriber_count": None, "hidden": True})
        published_at = parse_published_at(v["published_at"])
        score = compute_score(v["view_count"], ch["subscriber_count"], published_at, score_mode)
        rows.append(
            {
                "video_id": video_id,
                "title": v["title"],
                "channel_title": v["channel_title"],
                "subscriber_count": ch["subscriber_count"],
                "view_count": v["view_count"],
                "published_at": published_at,
                "score": score,
                "url": f"https://youtu.be/{video_id}",
            }
        )
    return rows


def assign_ranks_and_order(rows, sort_by):
    scored = [r for r in rows if r["score"] is not None]
    unscored = [r for r in rows if r["score"] is None]

    scored.sort(key=lambda r: r["score"], reverse=True)
    for i, r in enumerate(scored, start=1):
        r["rank"] = i
    for r in unscored:
        r["rank"] = "-"

    if sort_by == "score":
        unscored.sort(key=lambda r: r["view_count"], reverse=True)
        return scored + unscored
    if sort_by == "views":
        combined = scored + unscored
        combined.sort(key=lambda r: r["view_count"], reverse=True)
        return combined
    if sort_by == "date":
        combined = scored + unscored
        combined.sort(key=lambda r: r["published_at"], reverse=True)
        return combined
    raise ValueError(f"알 수 없는 sort_by: {sort_by}")


def print_table(rows):
    headers = ["순위", "제목", "채널", "구독자수", "조회수", "점수", "게시일", "URL"]
    table = []
    for r in rows:
        title = r["title"] if len(r["title"]) <= 45 else r["title"][:42] + "..."
        subs = "N/A" if r["subscriber_count"] is None else f"{r['subscriber_count']:,}"
        score = "N/A" if r["score"] is None else f"{r['score']:.2f}"
        table.append(
            [
                r["rank"],
                title,
                r["channel_title"],
                subs,
                f"{r['view_count']:,}",
                score,
                r["published_at"].strftime("%Y-%m-%d"),
                r["url"],
            ]
        )
    print(tabulate(table, headers=headers, tablefmt="github"))


def save_csv(rows, path):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["순위", "제목", "채널", "구독자수", "조회수", "점수", "게시일", "video_id", "URL"])
        for r in rows:
            writer.writerow(
                [
                    r["rank"],
                    r["title"],
                    r["channel_title"],
                    "N/A" if r["subscriber_count"] is None else r["subscriber_count"],
                    r["view_count"],
                    "N/A" if r["score"] is None else round(r["score"], 4),
                    r["published_at"].strftime("%Y-%m-%d"),
                    r["video_id"],
                    r["url"],
                ]
            )
    print(f"\nCSV 저장 완료: {path}")


def parse_args():
    parser = argparse.ArgumentParser(
        description="키워드로 유튜브 영상을 검색해 '구독자 대비 조회수'가 높은 숨은 떡상 영상을 찾는다.",
    )
    parser.add_argument("keyword", help="검색 키워드")
    parser.add_argument(
        "--api-key",
        default=os.environ.get("YOUTUBE_API_KEY"),
        help="YouTube Data API v3 키. 미지정 시 환경변수 YOUTUBE_API_KEY 사용",
    )
    parser.add_argument(
        "--order",
        choices=["viewCount", "date", "relevance"],
        default="viewCount",
        help="search.list 검색 순서 (기본: viewCount)",
    )
    parser.add_argument(
        "--sort-by",
        choices=["score", "views", "date"],
        default="score",
        help="결과 표 정렬 기준 (기본: score). 순위(rank) 열은 항상 점수 기준으로 매겨진다",
    )
    parser.add_argument(
        "--max-results",
        type=int,
        default=150,
        help=f"검색할 최대 영상 수 (기본: 150, 최대 {SEARCH_HARD_CAP}). 50개당 search.list 100유닛 소모",
    )
    parser.add_argument("--days", type=int, default=None, help="최근 N일 이내 게시된 영상만 검색")
    parser.add_argument("--min-views", type=int, default=0, help="최소 조회수 필터 (기본: 0)")
    parser.add_argument(
        "--min-subscribers",
        type=int,
        default=0,
        help="최소 구독자 수 필터 (기본: 0). 구독자가 너무 적은 채널(통계적 노이즈)을 걸러낼 때 사용. "
        "구독자 비공개 채널은 이 필터가 켜져 있으면 함께 제외됨",
    )
    parser.add_argument(
        "--max-subscribers",
        type=int,
        default=0,
        help="최대 구독자 수 필터 (기본: 0 = 제한 없음). '진짜 작은 채널'만 보고 싶을 때 사용 "
        "(예: 5000 = 구독자 5000명 이하 채널만). 구독자 비공개 채널은 이 필터가 켜져 있으면 함께 제외됨",
    )
    parser.add_argument(
        "--score-mode",
        choices=["simple", "recency"],
        default="simple",
        help="simple: 조회수/구독자수 | recency: (조회수/구독자수)/경과일수 (최근 영상 가중치)",
    )
    parser.add_argument("--csv", metavar="PATH", default=None, help="결과를 CSV 파일로 저장할 경로")
    return parser.parse_args()


def finish(quota):
    """실행 결과를 보고하고, 오늘 누적 사용량 파일에 반영해 함께 보여준다."""
    quota.report()
    cumulative = record_usage(quota.total_units)
    remaining = max(DAILY_QUOTA - cumulative, 0)
    print(f"오늘 누적 사용량(이 프로그램 기준) : {cumulative} / {DAILY_QUOTA} 유닛 (남은 유닛: {remaining})")


def main():
    args = parse_args()

    if not args.api_key:
        print(
            "오류: YouTube API 키가 필요합니다. --api-key 옵션을 쓰거나 "
            "환경변수 YOUTUBE_API_KEY를 설정하세요. (README.md 참고)",
            file=sys.stderr,
        )
        sys.exit(1)

    if args.max_results > SEARCH_HARD_CAP:
        print(
            f"참고: --max-results는 YouTube search.list 제약상 {SEARCH_HARD_CAP}개로 제한됩니다.",
            file=sys.stderr,
        )

    published_after = None
    if args.days is not None:
        published_after = (
            (datetime.now(timezone.utc) - timedelta(days=args.days))
            .isoformat()
            .replace("+00:00", "Z")
        )

    options = {
        "order": args.order,
        "sort_by": args.sort_by,
        "max_results": args.max_results,
        "days": args.days,
        "min_views": args.min_views,
        "min_subscribers": args.min_subscribers,
        "max_subscribers": args.max_subscribers,
        "score_mode": args.score_mode,
    }

    quota = QuotaTracker()
    client = YouTubeClient(args.api_key, quota)

    try:
        print(f"'{args.keyword}' 검색 중 (order={args.order})...")
        video_ids = client.search_video_ids(
            args.keyword, args.order, args.max_results, published_after
        )
        if not video_ids:
            print("검색 결과가 없습니다.")
            save_search_to_history(args.keyword, options, [], quota.total_units)
            finish(quota)
            return

        print(f"영상 상세정보 조회 중 ({len(video_ids)}개)...")
        video_details = client.get_videos_details(video_ids)

        channel_ids = sorted({v["channel_id"] for v in video_details.values()})
        print(f"채널 구독자 수 조회 중 ({len(channel_ids)}개)...")
        channel_details = client.get_channels_details(channel_ids)

    except ApiError as e:
        print(f"\n오류: {e}", file=sys.stderr)
        finish(quota)
        sys.exit(1)
    except requests.RequestException as e:
        print(f"\n네트워크 오류: {e}", file=sys.stderr)
        finish(quota)
        sys.exit(1)

    rows = build_rows(video_details, channel_details, args.score_mode)

    if args.min_views:
        rows = [r for r in rows if r["view_count"] >= args.min_views]

    if args.min_subscribers or args.max_subscribers:
        rows = [
            r
            for r in rows
            if r["subscriber_count"] is not None
            and (not args.min_subscribers or r["subscriber_count"] >= args.min_subscribers)
            and (not args.max_subscribers or r["subscriber_count"] <= args.max_subscribers)
        ]

    if not rows:
        print("\n필터 조건을 만족하는 영상이 없습니다.")
        save_search_to_history(args.keyword, options, [], quota.total_units)
        finish(quota)
        return

    rows = assign_ranks_and_order(rows, args.sort_by)

    print()
    print_table(rows)

    if args.csv:
        save_csv(rows, args.csv)

    save_search_to_history(args.keyword, options, rows, quota.total_units)
    finish(quota)


if __name__ == "__main__":
    main()
