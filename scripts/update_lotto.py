#!/usr/bin/env python3
"""Refresh the self-contained Lotto Insight HTML using verified public draw data.

Python 3.11+ and the standard library only. Failed downloads, invalid data,
conflicting past results, and incomplete publication never replace the file.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import time
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


KST = timezone(timedelta(hours=9))
FIRST_DRAW_DATE = date(2002, 12, 7)
FIRST_DRAW_AT = datetime(2002, 12, 7, 20, 45, tzinfo=KST)
OFFICIAL_HOME = "https://www.dhlottery.co.kr/lt645/result"
OFFICIAL_API = "https://www.dhlottery.co.kr/lt645/selectPstLt645Info.do"
MIRRORS = (
    "https://smok95.github.io/lotto/results/all.json",
    "https://papaya5rhw1984.github.io/lotto-data/all.json",
)
MAX_RESPONSE_BYTES = 10 * 1024 * 1024
DATA_SCRIPT = re.compile(
    r"<script\b(?=[^>]*\bid\s*=\s*([\"'])lotto-data\1)[^>]*>"
    r"(?P<payload>.*?)</script\s*>",
    re.IGNORECASE | re.DOTALL,
)


class DataError(ValueError):
    """Untrusted, missing, contradictory, or outdated draw data."""


def expected_round(now: datetime) -> int:
    """Latest scheduled drawing, with the Saturday 20:45 KST boundary."""
    if now.tzinfo is None:
        raise DataError("기준 시각에는 시간대가 필요합니다.")
    elapsed = now.astimezone(KST) - FIRST_DRAW_AT
    return max(0, elapsed.days // 7 + 1)


def integer(value: Any, label: str) -> int:
    if type(value) is int:
        return value
    if isinstance(value, str) and re.fullmatch(r"[0-9]+", value):
        return int(value)
    raise DataError(f"{label}: 정수가 아닙니다.")


def normalize_official(payload: Any) -> list[dict[str, Any]]:
    try:
        rows = payload["data"]["list"]
        if not isinstance(rows, list):
            raise TypeError
        return [
            {
                "round": integer(row["ltEpsd"], "회차"),
                "date": datetime.strptime(str(row["ltRflYmd"]), "%Y%m%d").date().isoformat(),
                "numbers": [integer(row[f"tm{i}WnNo"], "당첨번호") for i in range(1, 7)],
                "bonus": integer(row["bnsWnNo"], "보너스"),
            }
            for row in rows
        ]
    except (KeyError, TypeError, ValueError) as exc:
        raise DataError("동행복권 응답 형식이 변경되었거나 유효하지 않습니다.") from exc


def normalize_mirror(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, list):
        raise DataError("미러 응답은 전체 회차 배열이어야 합니다.")
    try:
        return [
            {
                "round": integer(row["draw_no"], "회차"),
                "date": str(row["date"])[:10],
                "numbers": row["numbers"],
                "bonus": integer(row["bonus_no"], "보너스"),
            }
            for row in payload
        ]
    except (KeyError, TypeError, ValueError) as exc:
        raise DataError("미러 응답 형식이 변경되었거나 유효하지 않습니다.") from exc


def validate_draws(
    rows: Any,
    now: datetime,
    *,
    require_latest: bool = False,
    allow_empty: bool = False,
) -> list[dict[str, Any]]:
    """Return canonical, ascending, contiguous records; reject every bad row."""
    if not isinstance(rows, list) or (not rows and not allow_empty):
        raise DataError("당첨 이력이 비어 있거나 배열 형식이 아닙니다.")
    ceiling = expected_round(now)
    canonical = []
    for row in rows:
        if not isinstance(row, dict):
            raise DataError("회차 데이터가 객체 형식이 아닙니다.")
        try:
            number = integer(row["round"], "회차")
            bonus = integer(row["bonus"], "보너스")
            values = row["numbers"]
            if not isinstance(values, list) or len(values) != 6:
                raise DataError(f"{number}회: 당첨번호는 6개여야 합니다.")
            numbers = [integer(value, "당첨번호") for value in values]
            if not 1 <= number <= ceiling:
                raise DataError(f"{number}회: 회차 범위 또는 미래 회차 오류입니다.")
            if len(set(numbers + [bonus])) != 7 or any(not 1 <= n <= 45 for n in numbers + [bonus]):
                raise DataError(f"{number}회: 중복 번호 또는 1~45 범위 오류입니다.")
            draw_date = row["date"]
            if not isinstance(draw_date, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", draw_date):
                raise DataError(f"{number}회: 추첨일 형식 오류입니다.")
            correct_date = FIRST_DRAW_DATE + timedelta(weeks=number - 1)
            if date.fromisoformat(draw_date) != correct_date:
                raise DataError(f"{number}회: 회차와 추첨일이 일치하지 않습니다.")
            canonical.append({"round": number, "date": draw_date, "numbers": sorted(numbers), "bonus": bonus})
        except (KeyError, TypeError, OverflowError, ValueError) as exc:
            if isinstance(exc, DataError):
                raise
            raise DataError("회차 데이터에 필수 필드가 없거나 값이 유효하지 않습니다.") from exc
    canonical.sort(key=lambda draw: draw["round"])
    for expected, draw in enumerate(canonical, start=1):
        if draw["round"] != expected:
            raise DataError(f"{expected}회부터 회차 누락 또는 중복이 있습니다.")
    if require_latest and (not canonical or canonical[-1]["round"] != ceiling):
        latest = canonical[-1]["round"] if canonical else 0
        raise DataError(f"현재 예상 {ceiling}회까지 필요하지만 {latest}회까지만 확인되었습니다.")
    return canonical


def fetch_json(url: str) -> Any:
    """Bounded public HTTPS request; no keys, cookies, or private credentials."""
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; LottoInsight/1.0)",
        "Accept": "application/json",
        "Referer": OFFICIAL_HOME,
    }
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            with urlopen(Request(url, headers=headers), timeout=20) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise DataError("응답이 허용 크기를 초과했습니다.")
            return json.loads(raw.decode("utf-8-sig"))
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt == 0:
                time.sleep(1)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DataError("데이터 원천이 JSON 대신 다른 내용을 반환했습니다.") from exc
    raise DataError(f"공개 데이터 요청 실패: {type(last_error).__name__}") from last_error


def assert_history_unchanged(old: list[dict[str, Any]], new: list[dict[str, Any]]) -> None:
    if len(new) < len(old):
        raise DataError("수집 이력이 기존 파일보다 짧아 덮어쓸 수 없습니다.")
    for before, after in zip(old, new):
        if before != after:
            raise DataError(f"기존 {before['round']}회와 수집 결과가 충돌합니다. 수동 확인이 필요합니다.")


def collect_draws(
    old: list[dict[str, Any]],
    now: datetime,
    fetch: Callable[[str], Any] = fetch_json,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    target = expected_round(now)
    official_url = OFFICIAL_API + "?" + urlencode({"srchStrLtEpsd": 1, "srchEndLtEpsd": target})
    official_error = ""
    try:
        official = validate_draws(normalize_official(fetch(official_url)), now, require_latest=True)
    except (DataError, OSError) as exc:
        official_error = str(exc)
    else:
        # A changed historical result is a hard stop, never a fallback trigger.
        assert_history_unchanged(old, official)
        return official, {
            "source": "동행복권 공식 전체 회차 데이터",
            "sourceUrl": OFFICIAL_HOME,
            "retrievalUrl": official_url,
            "verification": "official-full-history",
            "notes": "1회부터 최근 추첨 회차까지 공식 데이터의 회차·날짜·번호를 검증했습니다.",
        }

    mirrored = []
    for url in MIRRORS:
        try:
            mirrored.append(validate_draws(normalize_mirror(fetch(url)), now, require_latest=True))
        except (DataError, OSError) as exc:
            raise DataError(
                f"공식 조회 실패({official_error}). 미러 2곳 대조도 완료하지 못했습니다: {exc}"
            ) from exc
    if mirrored[0] != mirrored[1]:
        raise DataError("두 공개 미러의 회차·날짜·당첨번호가 서로 다릅니다. 원본을 유지합니다.")
    assert_history_unchanged(old, mirrored[0])
    return mirrored[0], {
        "source": "동행복권 당첨결과 공개 미러 2곳 교차 확인",
        "sourceUrl": MIRRORS[0],
        "crossCheckUrl": MIRRORS[1],
        "verification": "two-mirror-agreement",
        "notes": "공식 서버 조회 실패 시 공개 미러 2곳의 전체 회차가 일치할 때만 갱신합니다.",
    }


def read_embedded(html: str) -> tuple[dict[str, Any], tuple[int, int]]:
    matches = list(DATA_SCRIPT.finditer(html))
    if len(matches) != 1:
        raise DataError('id="lotto-data"인 데이터 script가 정확히 하나 있어야 합니다.')
    match = matches[0]
    opening = html[match.start():match.start("payload")]
    if not re.search(r'\btype\s*=\s*([\"\'])application/json\1', opening, re.IGNORECASE):
        raise DataError("데이터 script의 type은 application/json이어야 합니다.")
    try:
        payload = json.loads(match.group("payload"))
    except json.JSONDecodeError as exc:
        raise DataError("기존 HTML의 내장 JSON을 읽을 수 없습니다.") from exc
    if not isinstance(payload, dict) or "draws" not in payload or not isinstance(payload.get("meta"), dict):
        raise DataError("기존 데이터의 draws/meta 구조가 유효하지 않습니다.")
    return payload, match.span("payload")


def render_payload(html: str, span: tuple[int, int], payload: dict[str, Any]) -> str:
    serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    for character, escape in (("<", "\\u003c"), (">", "\\u003e"), ("&", "\\u0026"), ("\u2028", "\\u2028"), ("\u2029", "\\u2029")):
        serialized = serialized.replace(character, escape)
    return html[:span[0]] + serialized + html[span[1]:]


def atomic_write(path: Path, content: str) -> None:
    """Replace on the same filesystem only after flushing the complete file."""
    mode = stat.S_IMODE(path.stat().st_mode)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False) as file:
            temporary = file.name
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


def update_file(
    path: Path,
    now: datetime,
    fetch: Callable[[str], Any] = fetch_json,
    *,
    check_only: bool = False,
) -> dict[str, Any]:
    # Decode without universal newline conversion so unrelated content is exact.
    original = path.read_bytes().decode("utf-8")
    current, span = read_embedded(original)
    old = validate_draws(current["draws"], now, allow_empty=not check_only, require_latest=check_only)
    if check_only:
        return {"status": "valid", "rounds": len(old), "latest": old[-1]["round"], "changed": False}
    draws, provenance = collect_draws(old, now, fetch)
    if draws == old and current["meta"].get("coverageVerified") is True:
        return {"status": "current", "rounds": len(draws), "latest": draws[-1]["round"], "changed": False}
    payload = {
        "draws": draws,
        "meta": {
            **provenance,
            "updatedAt": now.astimezone(KST).isoformat(timespec="seconds"),
            "coverageVerified": True,
            "latestExpectedRound": expected_round(now),
            "firstRound": 1,
            "latestRound": draws[-1]["round"],
            "count": len(draws),
        },
    }
    updated = render_payload(original, span, payload)
    reloaded, _ = read_embedded(updated)
    validate_draws(reloaded["draws"], now, require_latest=True)
    atomic_write(path, updated)
    return {"status": "updated", "rounds": len(draws), "latest": draws[-1]["round"], "changed": True}


def parse_as_of(value: str) -> datetime:
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return datetime.fromisoformat(value + "T23:59:59").replace(tzinfo=KST)
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=KST)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("YYYY-MM-DD 또는 ISO 8601 시각을 사용하세요.") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", type=Path, default=Path(__file__).resolve().parents[1] / "index.html")
    parser.add_argument("--check-only", action="store_true", help="내장 이력의 완전성과 최신 회차만 검증; 네트워크/쓰기 없음")
    parser.add_argument("--as-of", type=parse_as_of, help="재현 검증용 기준 시각; 시간대 생략 시 KST")
    args = parser.parse_args()
    now = args.as_of or datetime.now(KST)
    try:
        print(json.dumps(update_file(args.file, now, check_only=args.check_only), ensure_ascii=False))
        return 0
    except (DataError, OSError, UnicodeDecodeError) as exc:
        print(f"갱신 중단 — 기존 HTML을 유지했습니다: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
