"""
scraper/live_score.py — CricEdge Live Score Fetcher

Primary source:  Cricbuzz.com (live-cricket-scores pages)
Secondary:       crex.live (if URL available from match list)

Fetches:
  - Current score, wickets, overs
  - Batsmen on strike (name, runs, balls)
  - ALL bowlers' figures for THIS match
  - Recent over history (last 3 overs, ball by ball)
  - Venue

Returns a structured dict or raises ScraperError.
If both sources fail, caller falls back to manual entry.
"""

import logging
import re
import json
from typing import Optional

import requests
from bs4 import BeautifulSoup

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

logger = logging.getLogger(__name__)


class ScraperError(Exception):
    pass


# ─────────────────────────────────────────────────────────────────
# REQUEST HELPER
# ─────────────────────────────────────────────────────────────────

def _get(url: str, timeout: int = None) -> BeautifulSoup:
    """Fetch URL and return BeautifulSoup object."""
    timeout = timeout or config.SCRAPER_TIMEOUT_SECONDS
    headers = {
        "User-Agent": config.SCRAPER_USER_AGENT,
        "Accept":     "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Connection": "keep-alive",
        "Referer":    "https://www.cricbuzz.com/",
    }
    try:
        resp = requests.get(url, headers=headers, timeout=timeout)
        resp.raise_for_status()
        return BeautifulSoup(resp.text, "lxml")
    except requests.RequestException as e:
        raise ScraperError(f"Request failed for {url}: {e}")


# ─────────────────────────────────────────────────────────────────
# CRICBUZZ — LIVE MATCH LIST
# ─────────────────────────────────────────────────────────────────

def get_live_matches_cricbuzz() -> list[dict]:
    """
    Scrape cricbuzz.com/cricket-match/live-scores for live matches.
    Cricbuzz renders match data in structured text — we parse the page text.
    """
    try:
        soup = _get(config.CRICBUZZ_LIVE_URL)
        matches = []
        seen_ids = set()  # dedupe by numeric match ID

        # Find all live-cricket-scores links — these are individual match pages
        all_links = soup.find_all("a", href=re.compile(r"/live-cricket-scores/\d+/", re.I))
        for link in all_links:
            href = link.get("href", "")
            if not href:
                continue

            # Extract numeric match ID for deduplication
            id_match = re.search(r"/live-cricket-scores/(\d+)/", href)
            if not id_match:
                continue
            match_id = id_match.group(1)
            if match_id in seen_ids:
                continue
            seen_ids.add(match_id)

            full_url = href if href.startswith("http") else f"https://www.cricbuzz.com{href}"

            # Extract team abbreviations from URL slug (most reliable)
            slug_match = re.search(r"/live-cricket-scores/\d+/([^/]+)", href)
            teams = ["Team A", "Team B"]
            title_str = ""
            if slug_match:
                slug = slug_match.group(1)
                vs_match = re.match(r"([a-z]+)-vs-([a-z]+)", slug)
                if vs_match:
                    teams = [vs_match.group(1).upper(), vs_match.group(2).upper()]
                title_str = slug.replace("-", " ").title()[:80]

            # Try to get a better title from link text or parent context
            link_text = link.get_text(" ", strip=True)
            if len(link_text) > 8:
                title_str = link_text[:80]

            # Score from link text
            score_m = re.search(r"(\d{1,3})\s*[-/]\s*(\d{1,2})\s*[\(\[]?\s*(\d{1,2}\.?\d?)", link_text)
            score_str = f"{score_m.group(1)}/{score_m.group(2)} ({score_m.group(3)})" if score_m else "Live"

            matches.append({
                "title":  title_str,
                "url":    full_url,
                "teams":  teams,
                "score":  score_str,
                "source": "cricbuzz",
            })

        logger.info("Cricbuzz: found %d live matches", len(matches))
        return matches

    except ScraperError as e:
        logger.warning("Cricbuzz match list failed: %s", e)
        return []


# ─────────────────────────────────────────────────────────────────
# CRICBUZZ — MATCH DETAIL
# ─────────────────────────────────────────────────────────────────

def fetch_match_cricbuzz(match_url: str) -> dict:
    """
    Scrape a Cricbuzz live match page and extract scorecard data.

    Cricbuzz now renders as a Next.js app with Tailwind CSS — class-based
    selectors no longer work. We parse the plain page text instead, which
    contains all the data in a predictable format.

    Score format in page text:
      "NZW 100 - 3 ( 13.4 ) CRR: 7.32"
    Batsman table:
      "Batter R B 4s 6s SR Sophie Devine * 27 17 3 0 158.82 Brooke Halliday 2 4 0 0 50.00"
    Bowler table:
      "Bowler O M R W ECO Nimasha Meepage * 2.4 0 17 1 6.40 Kavisha Dilhari 2 0 18 1 9.00"
    Venue:
      "Venue: The Rose Bowl, Southampton"
    Recent overs (from commentary):
      "Over 13 98-3 1 4 W 1 1 0 ( 7 runs)"
    """
    result = _empty_result()
    result["source"] = "cricbuzz"
    result["url"]    = match_url

    try:
        soup = _get(match_url)
        page_text = soup.get_text(" ", strip=True)

        # ── Score / innings / target ──────────────────────────────────────
        # Cricbuzz shows BOTH innings on a chase, first-innings first, e.g.:
        #   "NZW 150 - 6 ( 20 ) SLW 102 - 4 ( 14.4 ) CRR: 6.95 REQ: 9.19"
        # The team batting NOW is the score line immediately before "CRR" (the
        # current run rate marker); if there's no CRR (innings break / result),
        # fall back to the last distinct score line. The OTHER innings total is
        # the first-innings score, which gives the chase target.
        score_pat = r"([A-Z]{2,4}W?)\s+(\d{1,3})\s*[-/]\s*(\d{1,2})\s*\(\s*(\d{1,2}\.?\d?)\s*\)"
        crr_m = re.search(score_pat + r"\s*CRR", page_text)

        # All score lines in page order, de-duplicated by team abbreviation.
        distinct = []
        for ln in re.findall(score_pat, page_text):
            if ln[0] not in [d[0] for d in distinct]:
                distinct.append(ln)

        live = crr_m.groups() if crr_m else (distinct[-1] if distinct else None)
        if live:
            result["batting_abbr"] = live[0]
            result["runs"]    = int(live[1])
            result["wickets"] = int(live[2])
            result["overs"]   = float(live[3])
            others = [d for d in distinct if d[0] != live[0]]
            if others:
                # A completed other innings is shown → this is a chase (2nd inns).
                result["innings"] = 2
                result["target"]  = int(others[0][1]) + 1
            else:
                result["innings"] = 1
        else:
            # Fallback to the older prefix-less score formats if the structured
            # "ABBR runs - wkts ( overs )" lines weren't found.
            for pat in (
                r"(\d{1,3})\s*/\s*(\d{1,2})\s*\(\s*(\d{1,2}\.?\d?)\s*(?:ov|overs?)?\s*\)",
                r"(\d{1,3})\s*-\s*(\d{1,2})\s*\(\s*(\d{1,2}\.?\d?)\s*\)",
            ):
                m = re.search(pat, page_text)
                if m:
                    result["runs"]    = int(m.group(1))
                    result["wickets"] = int(m.group(2))
                    result["overs"]   = float(m.group(3))
                    break

        # Full batting-team name from the chase status line ("Sri Lanka Women
        # need 49 runs ...") — the most reliable batting-team signal when present.
        bat_name_m = re.search(
            r"([A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+)*?\s+Women)\s+need\b", page_text
        )
        if bat_name_m:
            result["batting_team_name"] = bat_name_m.group(1).strip()

        # Cross-check / fill target from "need X runs" if not already derived.
        need_m = re.search(r"need\s+(\d{1,3})\s+runs?", page_text, re.I)
        if need_m and result.get("runs"):
            result["innings"] = 2
            if not result.get("target"):
                result["target"] = result["runs"] + int(need_m.group(1))

        # ── Venue ────────────────────────────────────────────────────────
        venue_m = re.search(r"Venue\s*:\s*([^•\n\|]{5,60}?)(?:\s*•|\s*Date|\s*\||\s*$)", page_text)
        if venue_m:
            result["venue"] = venue_m.group(1).strip()

        # ── Batsmen ──────────────────────────────────────────────────────
        bat_section = re.search(
            r"Batter\s+R\s+B\s+4s\s+6s\s+SR\s+(.*?)(?:Bowler\s+O\s+M\s+R\s+W|Key Stats|Last Wkt|$)",
            page_text, re.S | re.I
        )
        if bat_section:
            bat_text = bat_section.group(1)
            # "Sophie Devine * 27 17 3 0 158.82" or "Brooke Halliday 2 4 0 0 50.00"
            bat_rows = re.findall(
                r"([A-Z][a-z]+(?:\s+[A-Z]?[a-z\-]+){1,3})\s*\*?\s+(\d{1,3})\s+(\d{1,3})\s+(\d{1,2})\s+(\d{1,2})\s+[\d\.]+",
                bat_text
            )
            for i, row in enumerate(bat_rows[:3]):
                name = row[0].strip()
                if len(name) < 4 or name.lower() in ("more", "batter", "bowler", "key stats"):
                    continue
                result["batsmen"].append({
                    "name":      name,
                    "runs":      int(row[1]),
                    "balls":     int(row[2]),
                    "fours":     int(row[3]),
                    "sixes":     int(row[4]),
                    "on_strike": i == 0,
                })

        # Mark on-strike batter using * indicator
        for i, batter in enumerate(result["batsmen"]):
            if re.search(re.escape(batter["name"]) + r"\s*\*", page_text):
                for j, b in enumerate(result["batsmen"]):
                    b["on_strike"] = (j == i)
                break

        # ── Bowlers ──────────────────────────────────────────────────────
        # "Bowler O M R W ECO Name * 2.4 0 17 1 6.40 Name 2 0 18 1 9.00"
        bowl_section = re.search(
            r"Bowler\s+O\s+M\s+R\s+W\s+ECO\s+(.*?)(?:Key Stats|Last Wkt|Partnership|Recent\s*:|Have Your Say|$)",
            page_text, re.S | re.I
        )
        if bowl_section:
            bowl_text = bowl_section.group(1)
            # "Kavisha Dilhari * 2.1 0 18 1 8.30" or "Nimasha Meepage 3 0 18 1 6.00"
            bowl_rows = re.findall(
                r"([A-Z][a-z]+(?:\s+[A-Z]?[a-z]+){1,3})\s*\*?\s+(\d{1,2}\.?\d?)\s+(\d{1,2})\s+(\d{1,3})\s+(\d{1,2})\s+([\d\.]+)",
                bowl_text
            )
            is_current_bowler = None
            for row in bowl_rows[:7]:
                name, overs_str, maidens, runs_str, wkts_str, eco_str = row
                name = name.strip()
                if len(name) < 4 or name in ("More", "Batter", "Bowler", "Key Stats"):
                    continue
                overs_f = float(overs_str)
                runs_g  = int(runs_str)
                wkts    = int(wkts_str)
                eco     = float(eco_str)
                bowler_d = {
                    "name":           name,
                    "overs_today":    overs_f,
                    "runs_today":     runs_g,
                    "wickets":        wkts,
                    "economy":        eco,
                    "overs_remaining": max(0, 4 - int(overs_f)),
                }
                result["all_bowlers"].append(bowler_d)
                if re.search(re.escape(name) + r"\s*\*", page_text) or (overs_f % 1 != 0):
                    is_current_bowler = bowler_d

            if is_current_bowler:
                result["current_bowler"] = is_current_bowler
            elif result["all_bowlers"]:
                result["current_bowler"] = result["all_bowlers"][-1]

        # ── Recent overs (from Over summary + commentary) ─────────────────
        # Format: "Over 13 98-3 1 4 W 1 1 0 ( 7 runs)"
        over_summaries = re.findall(
            r"Over\s+(\d{1,2})\s+\d+[-/]\d+\s+([\d\sW4wNB\.]+?)\s*\(",
            page_text, re.I
        )
        if over_summaries:
            for over_num, balls_str in over_summaries[-3:]:
                balls = [b.strip() for b in balls_str.split() if b.strip()]
                balls = [b for b in balls if re.match(r"^[\dW4wNB\.]+$", b, re.I)]
                if balls:
                    result["recent_overs"].append(balls)
        else:
            # Fallback: find "Recent : 1 0 1 0" pattern
            recent_m = re.search(r"Recent\s*:\s*([\d\sW4wNB]+)", page_text, re.I)
            if recent_m:
                balls = [b.strip() for b in recent_m.group(1).split() if b.strip()]
                if balls:
                    result["recent_overs"] = [balls]

        # ── Validate success ──────────────────────────────────────────────
        result["fetch_success"] = result["runs"] > 0 or result["overs"] > 0

        logger.info("Cricbuzz fetch OK: %d/%d in %.1f overs, venue=%s, %d batsmen, %d bowlers",
                    result["runs"], result["wickets"], result["overs"],
                    result.get("venue", "?"),
                    len(result["batsmen"]), len(result["all_bowlers"]))

    except Exception as e:
        logger.warning("Cricbuzz parse error: %s", e, exc_info=True)
        result["fetch_success"] = False

    return result


# ─────────────────────────────────────────────────────────────────
# CREX.LIVE — LIVE MATCH LIST (kept as secondary option)
# ─────────────────────────────────────────────────────────────────

def get_live_matches_crex() -> list[dict]:
    """
    Scrape crex.live for currently live matches.
    Returns list of {title, url, teams, score, status}.
    NOTE: crex.live frequently changes URL structure. Cricbuzz is primary.
    """
    try:
        # Try both URL variants
        crex_urls = [
            config.CREX_LIVE_MATCHES_URL,
            "https://crex.live/",
            "https://crex.live/fixtures/",
        ]
        soup = None
        for url in crex_urls:
            try:
                soup = _get(url)
                if soup:
                    break
            except ScraperError:
                continue

        if not soup:
            return []

        matches = []
        seen_urls = set()

        # crex.live match links
        all_links = soup.find_all("a", href=re.compile(r"/scorecard/|/match/|/live/", re.I))
        for link in all_links[:20]:
            href = link.get("href", "")
            if not href or href in seen_urls:
                continue
            seen_urls.add(href)
            full_url = href if href.startswith("http") else f"{config.CREX_LIVE_BASE_URL}{href}"
            text = link.get_text(" ", strip=True)
            if len(text) < 5:
                continue

            teams_m = re.findall(r"([A-Z][A-Za-z\s]+(?:XI|Women|W|U19)?)(?:\s+vs?\s+)([A-Z][A-Za-z\s]+(?:XI|Women|W|U19)?)", text)
            teams = list(teams_m[0]) if teams_m else ["Team A", "Team B"]

            score_m = re.search(r"(\d+/\d+)\s+\((\d+\.?\d*)", text)
            score = score_m.group(0) if score_m else "Live"

            matches.append({
                "title":  text[:80],
                "url":    full_url,
                "teams":  teams,
                "score":  score,
                "source": "crex.live",
            })

        logger.info("crex.live: found %d live matches", len(matches))
        return matches

    except Exception as e:
        logger.warning("crex.live match list failed: %s", e)
        return []


# ─────────────────────────────────────────────────────────────────
# CREX.LIVE — MATCH DETAIL
# ─────────────────────────────────────────────────────────────────

def fetch_match_crex(match_url: str) -> dict:
    """Scrape a crex.live match page."""
    soup = _get(match_url)
    result = _empty_result()
    result["source"] = "crex.live"
    result["url"]    = match_url

    try:
        page_text = soup.get_text(" ", strip=True)

        score_pattern = re.search(
            r"(\d{1,3})\s*/\s*(\d{1,2})\s+\(?\s*(\d{1,2}\.?\d?)\s*(?:overs?|ov)?\)?",
            page_text, re.I
        )
        if score_pattern:
            result["runs"]    = int(score_pattern.group(1))
            result["wickets"] = int(score_pattern.group(2))
            result["overs"]   = float(score_pattern.group(3))

        venue_m = re.search(r"Venue\s*:\s*([^•\n\|]{5,60}?)(?:\s*•|\s*Date|\s*\||\s*$)", page_text)
        if venue_m:
            result["venue"] = venue_m.group(1).strip()

        result["fetch_success"] = result["runs"] > 0

    except Exception as e:
        logger.warning("crex.live parse error: %s", e)
        result["fetch_success"] = False

    return result


# ─────────────────────────────────────────────────────────────────
# PUBLIC API
# ─────────────────────────────────────────────────────────────────

def get_live_matches() -> list[dict]:
    """
    Get list of live matches.
    Primary: Cricbuzz (most reliable, handles Women's T20 WC 2026).
    Fallback: crex.live.
    Returns combined list with source tagged.
    """
    matches = get_live_matches_cricbuzz()
    if not matches:
        logger.info("Cricbuzz returned no matches, trying crex.live...")
        matches = get_live_matches_crex()
    return matches


def fetch_live_data(match_url: str, source: str = "cricbuzz") -> dict:
    """
    Fetch full live data for a match.
    Routes to the correct scraper based on URL or source tag.
    Always returns a result dict (with fetch_success flag).
    """
    result = _empty_result()

    # Route based on URL
    if "cricbuzz" in match_url.lower():
        try:
            result = fetch_match_cricbuzz(match_url)
        except ScraperError as e:
            logger.warning("Cricbuzz fetch failed: %s", e)

    elif "crex" in match_url.lower():
        try:
            result = fetch_match_crex(match_url)
        except ScraperError as e:
            logger.warning("crex.live fetch failed: %s. Trying Cricbuzz...", e)

    # If fetch failed or no score, try the other source
    if not result.get("fetch_success"):
        if "cricbuzz" not in match_url.lower():
            # Try finding this match on Cricbuzz as fallback
            try:
                cb_matches = get_live_matches_cricbuzz()
                if cb_matches:
                    cb_result = fetch_match_cricbuzz(cb_matches[0]["url"])
                    if cb_result.get("fetch_success"):
                        result = cb_result
            except Exception as e:
                logger.error("Cricbuzz fallback also failed: %s", e)

    return result


def _empty_result() -> dict:
    """Return an empty result structure with all expected keys."""
    return {
        "fetch_success":  False,
        "source":         "none",
        "url":            "",
        "runs":           0,
        "wickets":        0,
        "overs":          0.0,
        "innings":        1,
        "target":         None,
        "batting_abbr":   None,   # scoreboard abbr of the team currently batting (e.g. "SLW")
        "batting_team_name": None, # full batting-team name from chase status line, if available
        "venue":          None,
        "batsmen":        [],    # [{"name", "runs", "balls", "fours", "sixes", "on_strike"}]
        "all_bowlers":    [],    # [{"name", "overs_today", "runs_today", "wickets", "economy", "overs_remaining"}]
        "current_bowler": None,
        "recent_overs":   [],    # [["4","1","W","0","6","2"], ...] last 3 overs
        "error":          None,
        "dismissed_batsmen": [],
    }


# ─────────────────────────────────────────────────────────────────
# REMAINING OVERS CALCULATOR
# ─────────────────────────────────────────────────────────────────

def calculate_remaining_bowlers(all_bowlers: list[dict], total_overs: int = 20) -> list[dict]:
    """
    Given all bowlers who have bowled, compute overs_remaining for each.
    T20: max 4 overs per bowler. Marks bowlers who still have quota left.
    """
    updated = []
    for b in all_bowlers:
        overs_bowled = b.get("overs_today", 0)
        overs_remaining = max(0, 4 - int(overs_bowled))
        updated.append({**b, "overs_remaining": overs_remaining})
    return [b for b in updated if b["overs_remaining"] > 0]


if __name__ == "__main__":
    # Quick test
    import logging
    logging.basicConfig(level=logging.INFO)
    print("Fetching live matches...")
    matches = get_live_matches()
    if matches:
        print(f"Found {len(matches)} live matches:")
        for m in matches[:5]:
            print(f"  {m['title'][:60]} — {m['source']}")
        print("\nFetching detail for first match...")
        detail = fetch_live_data(matches[0]["url"], source=matches[0].get("source","cricbuzz"))
        print(f"  Score: {detail['runs']}/{detail['wickets']} ({detail['overs']})")
        print(f"  Venue: {detail.get('venue')}")
        print(f"  Batsmen: {[b['name'] for b in detail['batsmen']]}")
        print(f"  Bowlers: {[b['name'] for b in detail['all_bowlers']]}")
        print(f"  Recent overs: {detail['recent_overs']}")
    else:
        print("No live matches found.")
