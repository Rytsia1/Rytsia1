#!/usr/bin/env python3
"""Generate an animated self-hosted RPG Character Sheet SVG for a GitHub profile."""

from __future__ import annotations

import base64
import html
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

ROOT_DIR = Path(__file__).resolve().parent.parent
OUTPUT_SVG = ROOT_DIR / "rpg-profile.svg"
ASSETS_DIR = ROOT_DIR / "assets"
GRAPHQL_URL = "https://api.github.com/graphql"

DEFAULT_USERNAME = "rytsia1"
DEFAULT_DISPLAY_NAME = "DIO"

CLASS_BY_LANGUAGE: dict[str, str] = {
    "Python": "QUANT ALCHEMIST",
    "C#": "UNITY PALADIN",
    "C++": "ENGINE ARCHITECT",
    "TypeScript": "FULLSTACK RANGER",
    "JavaScript": "DOM SORCERER",
    "HTML": "HTML ILLUSIONIST",
    "Vue": "REACTIVE ARTIFICER",
    "GDScript": "GODOT TACTICIAN",
    "Rust": "MEMORY WARDEN",
    "Go": "CONCURRENCY MONK",
    "Java": "JVM KNIGHT",
    "Dart": "FLUTTER BARD",
    "C": "KERNEL ARTISAN",
    "CSS": "STYLE ENCHANTER",
    "Shell": "TERMINAL SHAMAN",
}

LANGUAGE_FALLBACK_COLORS: dict[str, str] = {
    "Python": "#3572A5",
    "C#": "#178600",
    "C++": "#f34b7d",
    "TypeScript": "#3178c6",
    "JavaScript": "#f1e05a",
    "HTML": "#e34c26",
    "Vue": "#41b883",
    "GDScript": "#355570",
    "CSS": "#563d7c",
    "Shell": "#89e051",
}

GRAPHQL_QUERY = """
query($login: String!, $yearStart: DateTime!, $now: DateTime!) {
  user(login: $login) {
    login
    name
    createdAt
    followers {
      totalCount
    }
    pullRequests {
      totalCount
    }
    latestPushedRepos: repositories(
      first: 5
      ownerAffiliations: OWNER
      orderBy: { field: PUSHED_AT, direction: DESC }
    ) {
      nodes {
        nameWithOwner
        pushedAt
        defaultBranchRef {
          name
          target {
            ... on Commit {
              messageHeadline
              abbreviatedOid
              committedDate
            }
          }
        }
      }
    }
    yearStats: contributionsCollection(from: $yearStart, to: $now) {
      totalCommitContributions
      restrictedContributionsCount
      totalPullRequestReviewContributions
      contributionCalendar {
        totalContributions
        weeks {
          contributionDays {
            contributionCount
            date
            weekday
          }
        }
      }
    }
    rollingStats: contributionsCollection {
      totalPullRequestReviewContributions
      contributionCalendar {
        totalContributions
        weeks {
          contributionDays {
            contributionCount
            date
            weekday
          }
        }
      }
    }
    repositories(
      first: 100
      ownerAffiliations: OWNER
      isFork: false
      orderBy: { field: UPDATED_AT, direction: DESC }
    ) {
      pageInfo {
        hasNextPage
        endCursor
      }
      nodes {
        name
        languages(first: 10, orderBy: { field: SIZE, direction: DESC }) {
          edges {
            size
            node {
              name
              color
            }
          }
        }
      }
    }
  }
}
"""

REPOS_PAGE_QUERY = """
query($login: String!, $cursor: String!) {
  user(login: $login) {
    repositories(
      first: 100
      after: $cursor
      ownerAffiliations: OWNER
      isFork: false
      orderBy: { field: UPDATED_AT, direction: DESC }
    ) {
      pageInfo {
        hasNextPage
        endCursor
      }
      nodes {
        name
        languages(first: 10, orderBy: { field: SIZE, direction: DESC }) {
          edges {
            size
            node {
              name
              color
            }
          }
        }
      }
    }
  }
}
"""


def clamp_stat(value: float) -> int:
    """Clamp an attribute score into the 1..20 RPG range."""
    return max(1, min(20, int(round(value))))


def log_stat(count: int, ceiling: int) -> int:
    """Scale a non-negative count logarithmically to 1..20."""
    if count <= 0:
        return 1
    ratio = math.log1p(count) / math.log1p(ceiling)
    return clamp_stat(1 + 19 * min(1.0, ratio))


def compute_level_and_xp(total_xp: int) -> tuple[int, int, int, float]:
    """Derive RPG level and progress bar metrics from yearly contributions."""
    xp = max(0, total_xp)
    # Quadratic RPG curve: threshold(L) = 25 * (L - 1)^2
    level = 1 + int(math.sqrt(xp / 25)) if xp > 0 else 1
    curr_threshold = 25 * ((level - 1) ** 2)
    next_threshold = 25 * (level**2)
    span = max(1, next_threshold - curr_threshold)
    in_level = max(0, xp - curr_threshold)
    pct = min(1.0, in_level / span)
    return level, in_level, span, pct


def compute_streaks(weeks: list[dict[str, Any]]) -> tuple[int, int]:
    """Return (best_streak_days, active_days) from contribution weeks."""
    best = 0
    current = 0
    active = 0
    for week in weeks:
        for day in week.get("contributionDays", []):
            count = int(day.get("contributionCount", 0))
            if count > 0:
                active += 1
                current += 1
                if current > best:
                    best = current
            else:
                current = 0
    return best, active


def fetch_github_graphql(
    token: str, query: str, variables: dict[str, Any]
) -> dict[str, Any]:
    """Execute a GitHub GraphQL request and return the data payload."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/vnd.github+json",
    }
    resp = requests.post(
        GRAPHQL_URL,
        json={"query": query, "variables": variables},
        headers=headers,
        timeout=20,
    )
    resp.raise_for_status()
    payload = resp.json()
    if "errors" in payload:
        raise RuntimeError(f"GitHub GraphQL errors: {payload['errors']}")
    data = payload.get("data")
    if not data or not data.get("user"):
        raise RuntimeError(f"GitHub user not found in GraphQL response: {payload}")
    return data


def fetch_latest_push(username: str, token: str | None = None) -> dict[str, str] | None:
    """Fetch the user's most recent non-bot PushEvent from the GitHub REST API."""
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        resp = requests.get(
            f"https://api.github.com/users/{username}/events/public?per_page=30",
            headers=headers,
            timeout=15,
        )
        if not resp.ok:
            return None
        events = resp.json()
    except Exception:
        return None

    push_events = sorted(
        (ev for ev in events if ev.get("type") == "PushEvent"),
        key=lambda ev: ev.get("created_at", ""),
        reverse=True,
    )

    for ev in push_events:
        actor = (ev.get("actor") or {}).get("login", "")
        if actor.lower() == "github-actions[bot]":
            continue

        repo_name = (ev.get("repo") or {}).get("name", "")
        payload = ev.get("payload") or {}
        ref = payload.get("ref", "")
        branch = ref.removeprefix("refs/heads/") or "main"
        head_sha = payload.get("head", "")
        pushed_at = ev.get("created_at", "")

        commits = payload.get("commits") or []
        headline = ""
        if commits:
            headline = commits[-1].get("message", "").splitlines()[0].strip()
            head_sha = commits[-1].get("sha", head_sha)
        elif repo_name and head_sha:
            try:
                c_resp = requests.get(
                    f"https://api.github.com/repos/{repo_name}/commits/{head_sha}",
                    headers=headers,
                    timeout=10,
                )
                if c_resp.ok:
                    c_data = c_resp.json()
                    author_login = (c_data.get("author") or {}).get("login", "")
                    if author_login.lower() == "github-actions[bot]":
                        continue
                    msg = (c_data.get("commit") or {}).get("message", "")
                    headline = msg.splitlines()[0].strip()
            except Exception:
                pass

        if headline == "chore: update rpg-profile.svg":
            continue

        return {
            "repo": repo_name,
            "branch": branch,
            "sha": head_sha[:7] if head_sha else "HEAD",
            "message": headline or f"Pushed updates to {branch}",
            "pushed_at": pushed_at,
        }

    return None


def latest_push_from_repos(repo_nodes: list[dict[str, Any]]) -> dict[str, str] | None:
    """Fallback to the most recently pushed owned repository from GraphQL."""
    for repo in repo_nodes:
        branch_ref = repo.get("defaultBranchRef") or {}
        target = branch_ref.get("target") or {}
        headline = (target.get("messageHeadline") or "").strip()
        if headline == "chore: update rpg-profile.svg":
            continue
        return {
            "repo": repo.get("nameWithOwner", ""),
            "branch": branch_ref.get("name") or "main",
            "sha": target.get("abbreviatedOid") or "HEAD",
            "message": headline or "Pushed repository updates",
            "pushed_at": target.get("committedDate") or repo.get("pushedAt") or "",
        }
    return None


def collect_live_stats(username: str, token: str) -> dict[str, Any]:
    """Fetch real-time profile, language, PR, and contribution data."""
    now = datetime.now(timezone.utc)
    year_start = datetime(now.year, 1, 1, tzinfo=timezone.utc)
    variables = {
        "login": username,
        "yearStart": year_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "now": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    data = fetch_github_graphql(token, GRAPHQL_QUERY, variables)
    user = data["user"]

    repo_nodes = list(user["repositories"]["nodes"])
    page_info = user["repositories"]["pageInfo"]
    while page_info.get("hasNextPage") and page_info.get("endCursor"):
        next_data = fetch_github_graphql(
            token,
            REPOS_PAGE_QUERY,
            {"login": username, "cursor": page_info["endCursor"]},
        )
        repos_conn = next_data["user"]["repositories"]
        repo_nodes.extend(repos_conn["nodes"])
        page_info = repos_conn["pageInfo"]

    lang_totals: dict[str, dict[str, Any]] = {}
    for repo in repo_nodes:
        for edge in repo.get("languages", {}).get("edges", []):
            size = int(edge.get("size", 0))
            node = edge.get("node", {})
            name = node.get("name")
            if not name or size <= 0:
                continue
            color = node.get("color") or LANGUAGE_FALLBACK_COLORS.get(name, "#4ade80")
            if name not in lang_totals:
                lang_totals[name] = {"name": name, "color": color, "bytes": 0}
            lang_totals[name]["bytes"] += size

    total_bytes = sum(item["bytes"] for item in lang_totals.values())
    sorted_langs = sorted(
        lang_totals.values(), key=lambda x: x["bytes"], reverse=True
    )[:5]
    top_languages = [
        {
            "name": item["name"],
            "color": item["color"],
            "bytes": item["bytes"],
            "pct": (item["bytes"] / total_bytes * 100.0) if total_bytes > 0 else 0.0,
        }
        for item in sorted_langs
    ]

    created_at = datetime.fromisoformat(user["createdAt"].replace("Z", "+00:00"))
    account_age_years = max(0.1, (now - created_at).days / 365.25)

    year_stats = user["yearStats"]
    rolling_stats = user["rollingStats"]

    commits_this_year = int(year_stats["totalCommitContributions"]) + int(
        year_stats.get("restrictedContributionsCount", 0)
    )
    total_prs = int(user["pullRequests"]["totalCount"])
    pr_reviews = max(
        int(year_stats["totalPullRequestReviewContributions"]),
        int(rolling_stats["totalPullRequestReviewContributions"]),
    )
    followers = int(user["followers"]["totalCount"])

    year_weeks = year_stats["contributionCalendar"]["weeks"]
    rolling_weeks = rolling_stats["contributionCalendar"]["weeks"]
    year_xp = int(year_stats["contributionCalendar"]["totalContributions"])

    best_streak_rolling, _ = compute_streaks(rolling_weeks)
    best_streak_year, days_questing_year = compute_streaks(year_weeks)
    best_streak = max(best_streak_rolling, best_streak_year)

    latest_push = fetch_latest_push(username, token) or latest_push_from_repos(
        user.get("latestPushedRepos", {}).get("nodes", [])
    )

    return {
        "username": user["login"],
        "display_name": DEFAULT_DISPLAY_NAME if username.lower() == DEFAULT_USERNAME else (user.get("name") or username),
        "commits_year": commits_this_year,
        "total_prs": total_prs,
        "pr_reviews": pr_reviews,
        "account_age_years": account_age_years,
        "followers": followers,
        "best_streak": best_streak,
        "days_questing": days_questing_year,
        "year_xp": year_xp,
        "top_languages": top_languages,
        "latest_push": latest_push,
        "recent_weeks": rolling_weeks[-20:],
        "year_number": now.year,
        "source": "live",
    }


def build_fallback_stats(username: str) -> dict[str, Any]:
    """Build deterministic fallback stats for local testing without GITHUB_TOKEN."""
    now = datetime.now(timezone.utc)
    followers = 18
    account_age_years = 3.5

    # Try lightweight public REST lookup when online so local preview feels authentic.
    try:
        resp = requests.get(f"https://api.github.com/users/{username}", timeout=6)
        if resp.ok:
            u = resp.json()
            followers = int(u.get("followers", followers))
            if u.get("created_at"):
                created_at = datetime.fromisoformat(
                    u["created_at"].replace("Z", "+00:00")
                )
                account_age_years = max(0.1, (now - created_at).days / 365.25)
    except Exception:
        pass

    # Deterministic 20-week mock grid (20 weeks x 7 days)
    recent_weeks: list[dict[str, Any]] = []
    pattern = [0, 2, 5, 1, 8, 3, 0, 4, 11, 2, 0, 6, 9, 1]
    for w in range(20):
        days = []
        for d in range(7):
            val = pattern[(w * 7 + d) % len(pattern)]
            if w > 15 and d in (1, 2, 3, 4):
                val = max(val, 4 + (w % 4) * 2)
            days.append({"contributionCount": val, "date": "", "weekday": d})
        recent_weeks.append({"contributionDays": days})

    latest_push = fetch_latest_push(username) or {
        "repo": f"{username}/quant-alchemy",
        "branch": "main",
        "sha": "a1b2c3d",
        "message": "feat: implement adaptive quant momentum backtester",
        "pushed_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    return {
        "username": username,
        "display_name": DEFAULT_DISPLAY_NAME,
        "commits_year": 412,
        "total_prs": 28,
        "pr_reviews": 19,
        "account_age_years": account_age_years,
        "followers": followers,
        "best_streak": 24,
        "days_questing": 136,
        "year_xp": 684,
        "top_languages": [
            {"name": "Python", "color": "#3572A5", "bytes": 420000, "pct": 42.0},
            {"name": "C#", "color": "#178600", "bytes": 240000, "pct": 24.0},
            {"name": "C++", "color": "#f34b7d", "bytes": 160000, "pct": 16.0},
            {"name": "GDScript", "color": "#355570", "bytes": 110000, "pct": 11.0},
            {"name": "JavaScript", "color": "#f1e05a", "bytes": 70000, "pct": 7.0},
        ],
        "latest_push": latest_push,
        "recent_weeks": recent_weeks,
        "year_number": now.year,
        "source": "fallback",
    }


def load_avatar_data_uri(username: str) -> str | None:
    """Load local avatar from assets/ or fetch GitHub avatar and encode as Base64 URI."""
    candidates = [
        (ASSETS_DIR / "avatar.png", "image/png"),
        (ASSETS_DIR / "avatar.jpg", "image/jpeg"),
        (ASSETS_DIR / "avatar.jpeg", "image/jpeg"),
    ]
    for path, mime in candidates:
        if path.is_file():
            encoded = base64.b64encode(path.read_bytes()).decode("ascii")
            return f"data:{mime};base64,{encoded}"

    url = f"https://github.com/{username}.png?size=240"
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        content_type = resp.headers.get("Content-Type", "image/png").split(";")[0].strip()
        if not content_type.startswith("image/"):
            content_type = "image/png"
        encoded = base64.b64encode(resp.content).decode("ascii")
        return f"data:{content_type};base64,{encoded}"
    except Exception as exc:
        print(f"[warn] Could not fetch avatar from {url}: {exc}", file=sys.stderr)
        return None


def truncate(text: str, max_len: int) -> str:
    """Truncate text cleanly with ellipsis."""
    clean = " ".join(text.split())
    if len(clean) <= max_len:
        return clean
    return clean[: max(1, max_len - 3)].rstrip() + "..."


def wrap_text(text: str, width: int, max_lines: int = 2) -> list[str]:
    """Wrap text into at most `max_lines` lines without mid-word breaks when possible."""
    words = " ".join(text.split()).split(" ")
    if not words or words == [""]:
        return []
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) <= width:
            current = candidate
        else:
            if current:
                lines.append(current)
                current = word
            else:
                lines.append(truncate(word, width))
                current = ""
            if len(lines) == max_lines:
                break
    if len(lines) < max_lines and current:
        lines.append(current)
    elif len(lines) == max_lines and current:
        lines[-1] = truncate(f"{lines[-1]} {current}", width)
    return [truncate(line, width) for line in lines[:max_lines]]


def heat_color(count: int) -> str:
    """Map contribution count to emerald RPG heatmap color."""
    if count <= 0:
        return "#161d29"
    if count <= 2:
        return "#0e4429"
    if count <= 5:
        return "#15803d"
    if count <= 9:
        return "#22c55e"
    return "#4ade80"


def render_svg(stats: dict[str, Any], avatar_uri: str | None) -> str:
    """Render the animated RPG Character Sheet SVG."""
    username = html.escape(stats["username"])
    display_name = html.escape(stats["display_name"].upper())
    top_langs = stats["top_languages"]
    top_lang_name = top_langs[0]["name"] if top_langs else "Code"
    rpg_class = html.escape(CLASS_BY_LANGUAGE.get(top_lang_name, "CODE ADVENTURER"))

    # Compute 1..20 attributes
    attributes = [
        ("STR", log_stat(stats["commits_year"], 1500), f"{stats['commits_year']:,} commits this year"),
        ("DEX", log_stat(stats["total_prs"], 250), f"{stats['total_prs']:,} total PRs"),
        ("INT", log_stat(stats["pr_reviews"], 200), f"{stats['pr_reviews']:,} PR reviews"),
        ("WIS", clamp_stat(1 + 19 * min(1.0, stats["account_age_years"] / 10.0)), f"{stats['account_age_years']:.1f} years"),
        ("CHA", log_stat(stats["followers"], 200), f"{stats['followers']:,} followers"),
        ("CON", log_stat(stats["best_streak"], 120), f"{stats['best_streak']} day best streak"),
    ]

    level, in_level_xp, next_span, xp_pct = compute_level_and_xp(stats["year_xp"])
    xp_bar_max_w = 728
    xp_bar_w = max(8, int(round(xp_bar_max_w * xp_pct)))

    # Build attribute bars (20 discrete blocks per stat)
    attr_svg_parts: list[str] = []
    for row_idx, (code, score, tooltip) in enumerate(attributes):
        row_y = 224 + row_idx * 29
        attr_svg_parts.append(
            f'<g class="stat-row"><title>{html.escape(tooltip)}</title>'
            f'<text x="38" y="{row_y}" class="stat-code">{code}</text>'
        )
        for col_idx in range(20):
            bx = 86 + col_idx * 19
            by = row_y - 11
            if col_idx < score:
                delay = row_idx * 0.08 + col_idx * 0.025
                fill = "#4ade80" if col_idx >= 15 else "#22c55e"
                attr_svg_parts.append(
                    f'<rect x="{bx}" y="{by}" width="15" height="13" rx="2.5" '
                    f'fill="{fill}" class="stat-block" '
                    f'style="animation-delay:{delay:.3f}s"/>'
                )
            else:
                attr_svg_parts.append(
                    f'<rect x="{bx}" y="{by}" width="15" height="13" rx="2.5" '
                    f'fill="#192231" stroke="#233044" stroke-width="0.75"/>'
                )
        attr_svg_parts.append(
            f'<text x="510" y="{row_y}" text-anchor="end" class="stat-val">{score:02d}/20</text></g>'
        )

    # Build language inventory stacked bar & list
    inv_parts: list[str] = []
    stacked_x = 564.0
    stacked_total_w = 318.0
    if top_langs:
        inv_parts.append(
            '<rect x="564" y="212" width="318" height="9" rx="4.5" fill="#0d1117"/>'
            '<g clip-path="url(#lang-bar-clip)">'
        )
        pct_sum = sum(lang["pct"] for lang in top_langs) or 1.0
        for idx, lang in enumerate(top_langs):
            seg_w = (lang["pct"] / pct_sum) * stacked_total_w
            if idx == len(top_langs) - 1:
                seg_w = max(0.0, (564.0 + stacked_total_w) - stacked_x)
            inv_parts.append(
                f'<rect x="{stacked_x:.2f}" y="212" width="{seg_w:.2f}" height="9" '
                f'fill="{html.escape(lang["color"])}"/>'
            )
            stacked_x += seg_w
        inv_parts.append("</g>")

        for idx, lang in enumerate(top_langs):
            ly = 248 + idx * 30
            lcolor = html.escape(lang["color"])
            lname = html.escape(truncate(lang["name"], 15))
            lpct = lang["pct"]
            bar_w = max(4, int(round(115 * min(100.0, lpct) / 100.0)))
            inv_parts.append(
                f'<circle cx="570" cy="{ly - 4}" r="4.5" fill="{lcolor}"/>'
                f'<text x="583" y="{ly}" class="lang-name">{lname}</text>'
                f'<rect x="708" y="{ly - 8}" width="115" height="7" rx="3.5" fill="#0d1117" stroke="#1e293b" stroke-width="0.75"/>'
                f'<rect x="708" y="{ly - 8}" width="{bar_w}" height="7" rx="3.5" fill="{lcolor}" class="lang-fill" style="animation-delay:{0.15 + idx * 0.08:.2f}s"/>'
                f'<text x="882" y="{ly}" text-anchor="end" class="lang-pct">{lpct:.1f}%</text>'
            )
    else:
        inv_parts.append(
            '<text x="564" y="255" class="muted-text">NO OWNED REPOSITORY LANGUAGES FOUND</text>'
        )

    # Quest Log (Latest push)
    push = stats.get("latest_push")
    if push:
        repo_name = html.escape(truncate(push["repo"], 32))
        branch_name = html.escape(truncate(push.get("branch", "main"), 18))
        commit_sha = html.escape(push.get("sha", "HEAD")[:7])
        pushed_date = html.escape(str(push.get("pushed_at", ""))[:10] or "RECENT")
        title_lines = wrap_text(push["message"], width=39, max_lines=2)
        quest_badge = "[ ACTIVE QUEST // LATEST PUSH ]"
        quest_sub = f"{repo_name}  @{commit_sha}"
        quest_status_left = f"BRANCH: {branch_name}"
        quest_status_right = f"PUSHED: {pushed_date}"
    else:
        quest_badge = "[ SIDE QUEST // EXPLORATION ]"
        quest_sub = f"{username} // NO RECENT PUSHES"
        title_lines = ["Grinding commits, leveling skills,", "and forging new repositories."]
        quest_status_left = "STATUS: READY FOR QUEST"
        quest_status_right = "PARTY: SOLO"

    quest_title_svg = ""
    for line_idx, line_text in enumerate(title_lines):
        qy = 524 + line_idx * 19
        quest_title_svg += f'<text x="54" y="{qy}" class="quest-title">{html.escape(line_text)}</text>'

    # Adventure Log: 20 weeks x 7 days heatmap
    weeks = stats["recent_weeks"]
    if len(weeks) < 20:
        pad = [{"contributionDays": [{"contributionCount": 0} for _ in range(7)]} for _ in range(20 - len(weeks))]
        weeks = pad + weeks
    weeks = weeks[-20:]

    heatmap_parts: list[str] = []
    grid_x0 = 492
    grid_y0 = 460
    cell_size = 14
    cell_step = 18
    for w_idx, week in enumerate(weeks):
        days = week.get("contributionDays", [])
        for d_idx in range(7):
            count = int(days[d_idx].get("contributionCount", 0)) if d_idx < len(days) else 0
            date_str = str(days[d_idx].get("date", "")) if d_idx < len(days) else ""
            cx = grid_x0 + w_idx * cell_step
            cy = grid_y0 + d_idx * cell_step
            color = heat_color(count)
            stroke_attr = ' stroke="#243042" stroke-width="0.6"' if count == 0 else ""
            tip = f"{date_str}: {count} contributions" if date_str else f"{count} contributions"
            heatmap_parts.append(
                f'<rect x="{cx}" y="{cy}" width="{cell_size}" height="{cell_size}" rx="2.5" '
                f'fill="{color}"{stroke_attr}><title>{html.escape(tip)}</title></rect>'
            )

    # Avatar block
    if avatar_uri:
        avatar_svg = (
            '<clipPath id="avatar-clip">'
            '<rect x="39" y="37" width="88" height="88" rx="8"/>'
            "</clipPath>"
            f'<image x="39" y="37" width="88" height="88" preserveAspectRatio="xMidYMid slice" '
            f'clip-path="url(#avatar-clip)" href="{avatar_uri}" image-rendering="auto"/>'
        )
    else:
        avatar_svg = (
            '<rect x="39" y="37" width="88" height="88" rx="8" fill="#0d1117"/>'
            '<text x="83" y="88" text-anchor="middle" fill="#4ade80" font-size="28" font-weight="700">D</text>'
        )

    class_label_text = f"CLASS // {rpg_class}"
    class_pill_w = max(210, int(len(class_label_text) * 8.8 + 24))

    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="920" height="656" viewBox="0 0 920 656" role="img" aria-label="{display_name} RPG Character Sheet">
  <defs>
    <clipPath id="lang-bar-clip">
      <rect x="564" y="212" width="318" height="9" rx="4.5"/>
    </clipPath>
    <linearGradient id="xp-grad" x1="0%" y1="0%" x2="100%" y2="0%">
      <stop offset="0%" stop-color="#16a34a"/>
      <stop offset="55%" stop-color="#22c55e"/>
      <stop offset="100%" stop-color="#4ade80"/>
    </linearGradient>
    <linearGradient id="header-glow" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#22c55e" stop-opacity="0.14"/>
      <stop offset="100%" stop-color="#0d1117" stop-opacity="0"/>
    </linearGradient>
    <pattern id="bg-grid" width="24" height="24" patternUnits="userSpaceOnUse">
      <path d="M 24 0 L 0 0 0 24" fill="none" stroke="#162030" stroke-width="0.6" opacity="0.5"/>
    </pattern>
  </defs>

  <style>
    text {{
      font-family: 'JetBrains Mono', 'Fira Code', 'SFMono-Regular', Consolas, 'Liberation Mono', Menlo, monospace;
    }}
    .hero-name {{ fill: #f8fafc; font-size: 23px; font-weight: 800; letter-spacing: 1px; }}
    .hero-handle {{ fill: #64748b; font-size: 13px; font-weight: 500; }}
    .class-badge {{ fill: #4ade80; font-size: 12px; font-weight: 700; letter-spacing: 1.4px; animation: emeraldPulse 3s ease-in-out infinite; }}
    .lvl-num {{ fill: #4ade80; font-size: 22px; font-weight: 800; letter-spacing: 1px; animation: emeraldPulse 3s ease-in-out infinite; }}
    .lvl-sub {{ fill: #64748b; font-size: 10px; font-weight: 600; letter-spacing: 1px; }}
    .xp-text {{ fill: #94a3b8; font-size: 11px; font-weight: 600; }}
    .xp-pct {{ fill: #4ade80; font-size: 11px; font-weight: 700; }}
    .section-title {{ fill: #4ade80; font-size: 12px; font-weight: 700; letter-spacing: 1.2px; animation: titleGlow 3.2s ease-in-out infinite; }}
    .stat-code {{ fill: #f1f5f9; font-size: 12.5px; font-weight: 700; }}
    .stat-val {{ fill: #4ade80; font-size: 11.5px; font-weight: 700; }}
    .lang-name {{ fill: #e2e8f0; font-size: 12px; font-weight: 600; }}
    .lang-pct {{ fill: #94a3b8; font-size: 11px; font-weight: 600; }}
    .quest-badge {{ fill: #4ade80; font-size: 10.5px; font-weight: 700; letter-spacing: 0.9px; }}
    .quest-sub {{ fill: #94a3b8; font-size: 11px; font-weight: 600; }}
    .quest-title {{ fill: #f8fafc; font-size: 12.5px; font-weight: 700; }}
    .quest-foot {{ fill: #64748b; font-size: 10.5px; font-weight: 600; }}
    .day-label {{ fill: #64748b; font-size: 10px; font-weight: 600; }}
    .summary-label {{ fill: #64748b; font-size: 9.5px; font-weight: 700; letter-spacing: 0.8px; }}
    .summary-val {{ fill: #4ade80; font-size: 14px; font-weight: 800; }}
    .muted-text {{ fill: #64748b; font-size: 11px; }}

    .stat-block {{
      opacity: 0;
      transform-box: fill-box;
      transform-origin: center;
      animation: blockPop 0.32s cubic-bezier(0.22, 1, 0.36, 1) forwards;
    }}
    .xp-fill {{
      transform-box: fill-box;
      transform-origin: left center;
      animation: xpFill 1.2s cubic-bezier(0.22, 1, 0.36, 1) forwards;
    }}
    .lang-fill {{
      transform-box: fill-box;
      transform-origin: left center;
      animation: xpFill 0.9s cubic-bezier(0.22, 1, 0.36, 1) both;
    }}

    @keyframes blockPop {{
      0% {{ opacity: 0; transform: scale(0.4); }}
      70% {{ opacity: 1; transform: scale(1.08); }}
      100% {{ opacity: 1; transform: scale(1); }}
    }}
    @keyframes xpFill {{
      0% {{ transform: scaleX(0); opacity: 0.4; }}
      100% {{ transform: scaleX(1); opacity: 1; }}
    }}
    @keyframes emeraldPulse {{
      0%, 100% {{ opacity: 0.88; fill: #4ade80; }}
      50% {{ opacity: 1; fill: #86efac; }}
    }}
    @keyframes titleGlow {{
      0%, 100% {{ opacity: 0.85; }}
      50% {{ opacity: 1; }}
    }}
  </style>

  <!-- Base Canvas -->
  <rect width="920" height="656" rx="14" fill="#0d1117"/>
  <rect width="920" height="656" rx="14" fill="url(#bg-grid)"/>
  <rect x="1" y="1" width="918" height="654" rx="13" fill="none" stroke="#1e293b" stroke-width="1.5"/>

  <!-- Header Card -->
  <rect x="20" y="18" width="880" height="126" rx="10" fill="#131924" stroke="#1e293b" stroke-width="1.2"/>
  <rect x="20" y="18" width="880" height="126" rx="10" fill="url(#header-glow)"/>
  <!-- Emerald HUD corners -->
  <path d="M 20 34 L 20 22 Q 20 18 24 18 L 36 18" fill="none" stroke="#22c55e" stroke-width="2"/>
  <path d="M 884 18 L 896 18 Q 900 18 900 22 L 900 34" fill="none" stroke="#22c55e" stroke-width="2"/>

  <!-- Avatar Box -->
  <rect x="36" y="34" width="94" height="94" rx="10" fill="#0d1117" stroke="#22c55e" stroke-width="1.8"/>
  {avatar_svg}

  <!-- Character Identity -->
  <text x="148" y="57" class="hero-name">{display_name} <tspan class="hero-handle">@{username}</tspan></text>
  <rect x="148" y="67" width="{class_pill_w}" height="21" rx="4" fill="#0d1117" stroke="#22c55e" stroke-opacity="0.55" stroke-width="1"/>
  <text x="158" y="81.5" class="class-badge">{class_label_text}</text>

  <!-- Level Readout -->
  <text x="880" y="57" text-anchor="end" class="lvl-num">LVL {level}</text>
  <text x="880" y="74" text-anchor="end" class="lvl-sub">CHRONICLE {stats["year_number"]}</text>

  <!-- XP Progress Bar -->
  <rect x="148" y="96" width="732" height="14" rx="7" fill="#0d1117" stroke="#1e293b" stroke-width="1"/>
  <rect x="150" y="98" width="{xp_bar_w}" height="10" rx="5" fill="url(#xp-grad)" class="xp-fill"/>
  <text x="148" y="128" class="xp-text">XP {in_level_xp:,} / {next_span:,}  (TOTAL {stats["year_xp"]:,} THIS YEAR)</text>
  <text x="880" y="128" text-anchor="end" class="xp-pct">{xp_pct * 100:.0f}% TO LVL {level + 1}</text>

  <!-- Left Card: Character Attributes -->
  <rect x="20" y="160" width="510" height="232" rx="10" fill="#131924" stroke="#1e293b" stroke-width="1.2"/>
  <text x="38" y="186" class="section-title">&gt; CHARACTER ATTRIBUTES</text>
  <line x1="38" y1="198" x2="512" y2="198" stroke="#1e293b" stroke-width="1"/>
  {"".join(attr_svg_parts)}

  <!-- Right Card: Inventory / Top Languages -->
  <rect x="546" y="160" width="354" height="232" rx="10" fill="#131924" stroke="#1e293b" stroke-width="1.2"/>
  <text x="564" y="186" class="section-title">&gt; INVENTORY // LANGUAGES</text>
  <line x1="564" y1="198" x2="882" y2="198" stroke="#1e293b" stroke-width="1"/>
  {"".join(inv_parts)}

  <!-- Bottom Left Card: Quest Log -->
  <rect x="20" y="408" width="380" height="230" rx="10" fill="#131924" stroke="#1e293b" stroke-width="1.2"/>
  <text x="38" y="434" class="section-title">&gt; QUEST LOG</text>
  <line x1="38" y1="446" x2="382" y2="446" stroke="#1e293b" stroke-width="1"/>
  <rect x="38" y="460" width="344" height="114" rx="8" fill="#0d1117" stroke="#22c55e" stroke-opacity="0.45" stroke-width="1.1"/>
  <text x="54" y="481" class="quest-badge">{quest_badge}</text>
  <text x="54" y="501" class="quest-sub">{quest_sub}</text>
  {quest_title_svg}
  <rect x="38" y="586" width="344" height="36" rx="6" fill="#0d1117" stroke="#1e293b" stroke-width="1"/>
  <text x="52" y="608" class="quest-foot">{quest_status_left}</text>
  <text x="368" y="608" text-anchor="end" class="quest-foot">{quest_status_right}</text>

  <!-- Bottom Right Card: Adventure Log -->
  <rect x="416" y="408" width="484" height="230" rx="10" fill="#131924" stroke="#1e293b" stroke-width="1.2"/>
  <text x="434" y="434" class="section-title">&gt; ADVENTURE LOG (20W)</text>
  <text x="762" y="433" text-anchor="end" class="day-label">LESS</text>
  <rect x="770" y="424" width="9" height="9" rx="2" fill="#161d29" stroke="#243042" stroke-width="0.6"/>
  <rect x="782" y="424" width="9" height="9" rx="2" fill="#0e4429"/>
  <rect x="794" y="424" width="9" height="9" rx="2" fill="#15803d"/>
  <rect x="806" y="424" width="9" height="9" rx="2" fill="#22c55e"/>
  <rect x="818" y="424" width="9" height="9" rx="2" fill="#4ade80"/>
  <text x="835" y="433" class="day-label">MORE</text>
  <line x1="434" y1="446" x2="882" y2="446" stroke="#1e293b" stroke-width="1"/>

  <!-- Weekday Labels -->
  <text x="442" y="489" class="day-label">MON</text>
  <text x="442" y="525" class="day-label">WED</text>
  <text x="442" y="561" class="day-label">FRI</text>

  <!-- 20-Week Contribution Heatmap -->
  {"".join(heatmap_parts)}

  <!-- Adventure Summary Pills -->
  <rect x="434" y="586" width="142" height="36" rx="6" fill="#0d1117" stroke="#1e293b" stroke-width="1"/>
  <text x="446" y="600" class="summary-label">DAYS QUESTING</text>
  <text x="446" y="616" class="summary-val">{stats["days_questing"]}d</text>

  <rect x="587" y="586" width="142" height="36" rx="6" fill="#0d1117" stroke="#1e293b" stroke-width="1"/>
  <text x="599" y="600" class="summary-label">LONGEST QUEST</text>
  <text x="599" y="616" class="summary-val">{stats["best_streak"]}d</text>

  <rect x="740" y="586" width="142" height="36" rx="6" fill="#0d1117" stroke="#1e293b" stroke-width="1"/>
  <text x="752" y="600" class="summary-label">XP THIS YEAR</text>
  <text x="752" y="616" class="summary-val">{stats["year_xp"]:,}</text>
</svg>
'''
    return svg


def main() -> int:
    username = os.getenv("GITHUB_REPOSITORY_OWNER") or DEFAULT_USERNAME
    token = os.getenv("GITHUB_TOKEN", "").strip()

    if token:
        stats = collect_live_stats(username, token)
    else:
        print(
            "[info] GITHUB_TOKEN not set; generating RPG profile using local fallback data.",
            file=sys.stderr,
        )
        stats = build_fallback_stats(username)

    avatar_uri = load_avatar_data_uri(username)
    svg_content = render_svg(stats, avatar_uri)
    OUTPUT_SVG.write_text(svg_content, encoding="utf-8")
    print(f"Generated {OUTPUT_SVG} ({stats['source']} mode, user={username}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
