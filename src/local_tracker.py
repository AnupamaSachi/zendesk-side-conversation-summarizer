#!/usr/bin/env python3
"""Local Zendesk side-conversation summarizer.

This template reads configured Zendesk tickets, sends only side-conversation
text to a locally authenticated Codex CLI, and writes JSON/CSV results. All
company-specific values belong in local configuration files, never in source.
"""

import argparse
import csv
import json
import os
import re
import subprocess
import sys
from base64 import b64encode
from datetime import datetime, timezone, timedelta
from urllib.parse import quote
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(ROOT)
CODEX = ""
PRODUCT_FIELD_ID = ""
PRODUCTS = {}
USER_CACHE = {}
USER_DETAIL_CACHE = {}


def load_env():
    path = os.path.join(PROJECT_ROOT, ".env")
    if not os.path.exists(path):
        raise RuntimeError("Create a .env file in the project folder first.")
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


def load_product_config():
    """Load non-secret product mappings from local configuration."""
    global CODEX, PRODUCT_FIELD_ID, PRODUCTS
    config_path = os.path.join(PROJECT_ROOT, "config", "products.json")
    with open(config_path, encoding="utf-8") as handle:
        config = json.load(handle)
    CODEX = os.environ.get(
        "CODEX_CLI_PATH", "/Applications/ChatGPT.app/Contents/Resources/codex"
    )
    PRODUCT_FIELD_ID = str(
        os.environ.get("ZENDESK_PRODUCT_FIELD_ID", config.get("zendesk_product_field_id", ""))
    )
    PRODUCTS = {
        item["value"]: item["name"]
        for group in config.get("products", {}).values()
        for item in group
    }


def include_tag_query():
    include_tag = os.environ.get("ZENDESK_INCLUDE_TAG", "").strip()
    exclude_tag = os.environ.get("ZENDESK_EXCLUDE_TAG", "").strip()
    parts = []
    if include_tag:
        parts.append("tags:" + include_tag)
    if exclude_tag:
        parts.append("-tags:" + exclude_tag)
    return " ".join(parts)


def zendesk_request(path):
    subdomain = os.environ["ZENDESK_SUBDOMAIN"]
    user = os.environ["ZENDESK_EMAIL"] + "/token"
    token = os.environ["ZENDESK_API_TOKEN"]
    auth = b64encode((user + ":" + token).encode()).decode()
    request = Request(
        "https://" + subdomain + ".zendesk.com" + path,
        headers={"Authorization": "Basic " + auth, "Accept": "application/json"},
    )
    with urlopen(request, timeout=45) as response:
        return json.loads(response.read().decode("utf-8"))


def zendesk_search_all(query):
    """Return every Zendesk search result, not only the first 100 records."""
    results = []
    page = 1
    while True:
        path = (
            "/api/v2/search.json?query=" + quote(query)
            + "&per_page=100&page=" + str(page)
        )
        data = zendesk_request(path)
        page_results = data.get("results", []) or []
        results.extend(page_results)
        if not data.get("next_page") or not page_results:
            break
        page += 1
    return results


def jira_request(path):
    """Read one Jira resource using the local .env credentials."""
    base_url = os.environ.get("JIRA_BASE_URL", "").rstrip("/")
    email = os.environ.get("JIRA_EMAIL", "").strip()
    token = os.environ.get("JIRA_API_TOKEN", "").strip()
    if not base_url or not email or not token:
        return {}
    auth = b64encode((email + ":" + token).encode()).decode()
    request = Request(
        base_url + path,
        headers={"Authorization": "Basic " + auth, "Accept": "application/json"},
    )
    with urlopen(request, timeout=45) as response:
        return json.loads(response.read().decode("utf-8"))


def search_recent_tickets(hours):
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    found = {}
    for product_value in PRODUCTS:
        query = (
            "type:ticket updated>" + str(hours) + "hours "
            "custom_field_" + PRODUCT_FIELD_ID + ":" + product_value + " "
            + include_tag_query()
        )
        for item in zendesk_search_all(query):
            found[str(item["id"])] = item
    return list(found.values())


def search_tickets_created_since(start_date):
    """Find tickets updated on or after YYYY-MM-DD for side-conversation filtering."""
    found = {}
    for product_value in PRODUCTS:
        query = (
            "type:ticket updated>=" + str(start_date) + " "
            "custom_field_" + PRODUCT_FIELD_ID + ":" + product_value + " "
            + include_tag_query()
        )
        for item in zendesk_search_all(query):
            # The search is based on updated date; main() applies the actual
            # side-conversation submitted-date cutoff after loading conversations.
            found[str(item["id"])] = item
    return list(found.values())


def get_side_conversation_date(conversations, fallback=""):
    """Return the earliest submitted/created side-conversation date."""
    timestamps = []
    for conversation in conversations:
        timestamp = (
            conversation.get("created_at")
            or conversation.get("submitted_at")
            or conversation.get("createdAt")
            or ""
        )
        if timestamp:
            timestamps.append(str(timestamp))
    return min(timestamps)[:10] if timestamps else str(fallback or "")[:10]


def get_ticket(ticket_id):
    return zendesk_request("/api/v2/tickets/" + str(ticket_id) + ".json")["ticket"]


def get_assignee_name(ticket):
    assignee = ticket.get("assignee") or {}
    if assignee.get("name"):
        return assignee["name"]
    assignee_id = ticket.get("assignee_id")
    if not assignee_id:
        return ""
    key = str(assignee_id)
    if key not in USER_CACHE:
        try:
            USER_CACHE[key] = zendesk_request(
                "/api/v2/users/" + key + ".json"
            ).get("user", {}).get("name", "")
        except Exception as error:
            print("Assignee lookup skipped:", error, file=sys.stderr)
            USER_CACHE[key] = ""
    return USER_CACHE[key]


def get_user_details(user_id):
    """Return a cached Zendesk user record for comment-author matching."""
    key = str(user_id or "")
    if not key:
        return {}
    if key not in USER_DETAIL_CACHE:
        try:
            USER_DETAIL_CACHE[key] = zendesk_request(
                "/api/v2/users/" + key + ".json"
            ).get("user", {})
        except Exception as error:
            print("User lookup skipped:", error, file=sys.stderr)
            USER_DETAIL_CACHE[key] = {}
    return USER_DETAIL_CACHE[key]


def get_side_conversations(ticket_id):
    try:
        data = zendesk_request(
            "/api/v2/tickets/" + str(ticket_id) + "/side_conversations.json"
        )
        conversations = data.get("side_conversations", [])
        for conversation in conversations:
            external_ids = conversation.get("external_ids") or {}
            target_ticket_id = external_ids.get("targetTicketId")
            if not target_ticket_id:
                continue
            try:
                linked_ticket = get_ticket(target_ticket_id)
                conversation["linked_ticket_subject"] = linked_ticket.get("subject", "")
                conversation["linked_ticket_description"] = linked_ticket.get("description", "")
            except Exception as error:
                print("Linked side-conversation ticket lookup skipped:", error, file=sys.stderr)
        return conversations
    except Exception as error:
        print("Side-conversation lookup skipped:", error, file=sys.stderr)
        return []


def is_external_escalation(ticket):
    excluded_tag = os.environ.get("ZENDESK_EXCLUDE_TAG", "").strip()
    return bool(excluded_tag and excluded_tag in set(ticket.get("tags") or []))


def get_parent_ticket_id(ticket):
    source = ticket.get("via", {}).get("source", {})
    parent_id = (source.get("from") or {}).get("ticket_id")
    if parent_id:
        return str(parent_id)
    text = " ".join([ticket.get("subject", ""), ticket.get("description", "")])
    match = re.search(r"side conversation from ticket\s*#?(\d+)", text, re.I)
    return match.group(1) if match else ""


def get_lead_response(ticket_id, conversations):
    responses = []
    for conversation in conversations:
        external_ids = conversation.get("external_ids") or {}
        target_id = external_ids.get("targetTicketId") or conversation.get("ticket_id")
        if not target_id:
            continue
        try:
            comments = zendesk_request(
                "/api/v2/tickets/" + str(target_id) + "/comments.json?sort_order=asc&per_page=100"
            ).get("comments", [])
            for comment in comments:
                author = comment.get("author") or {}
                if not author and comment.get("author_id"):
                    author = get_user_details(comment.get("author_id"))
                name = " ".join(str(author.get(k, "")) for k in ("name", "email"))
                author_pattern = os.environ.get("LEAD_RESPONSE_AUTHOR_PATTERN", "").strip()
                if author_pattern and re.search(author_pattern, name, re.I):
                    body = clean_text(comment.get("body") or comment.get("plain_body"))
                    if body and not is_automated_text(body):
                        responses.append(body)
        except Exception as error:
            print("Lead-response lookup skipped:", error, file=sys.stderr)
    return " ".join(responses)[-2000:]


def is_automated_text(text):
    return bool(re.search(
        r"auto[- ]?remediation|automated\s+(diagnosis|remediation|response)|why human review is needed|ai-generated|generated by automation",
        str(text or ""), re.I
    ))


def get_product(ticket):
    for field in ticket.get("custom_fields", []):
        if str(field.get("id")) == PRODUCT_FIELD_ID:
            value = field.get("value")
            return PRODUCTS.get(value, "")
    return ""


def jira_text(value):
    """Flatten Jira Atlassian Document Format or plain text into readable text."""
    if isinstance(value, str):
        return clean_text(value)
    parts = []

    def walk(node):
        if isinstance(node, dict):
            if isinstance(node.get("text"), str):
                parts.append(node["text"])
            for child in node.get("content", []) or []:
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(value)
    return clean_text(" ".join(parts))


def get_jira_info(ticket):
    """Return Jira ID, latest human developer response, and Jira status."""
    issue_key = ""
    jira_field_id = os.environ.get("JIRA_ISSUE_FIELD_ID", "").strip()
    for field in ticket.get("custom_fields", []):
        if jira_field_id and str(field.get("id")) == jira_field_id:
            issue_key = clean_text(field.get("value"))
            break
    if not issue_key:
        return {"jira_id": "", "dev_response": "", "jira_case_status": ""}

    try:
        issue = jira_request(
            "/rest/api/3/issue/" + quote(issue_key, safe="")
            + "?fields=summary,status,comment"
        )
        fields = issue.get("fields", {}) or {}
        status = clean_text((fields.get("status") or {}).get("name"))
        comments = ((fields.get("comment") or {}).get("comments")) or []
        human_responses = []
        for comment in comments:
            body = jira_text(comment.get("body"))
            author = comment.get("author") or {}
            author_name = clean_text(
                author.get("displayName") or author.get("emailAddress") or ""
            )
            automated_author = bool(re.search(
                r"bot|automation|codex|gemini|ai|system", author_name, re.I
            ))
            if body and not automated_author and not is_automated_text(body):
                human_responses.append(body)
        return {
            "jira_id": issue_key,
            "dev_response": human_responses[-1][:3000] if human_responses else "",
            "jira_case_status": status,
        }
    except Exception as error:
        print("Jira lookup skipped for " + issue_key + ": " + str(error), file=sys.stderr)
        return {"jira_id": issue_key, "dev_response": "", "jira_case_status": ""}


def clean_text(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def call_codex(ticket):
    if not os.path.exists(CODEX):
        raise RuntimeError("Codex CLI was not found at: " + CODEX)

    side_parts = []
    for item in ticket["side_conversations"]:
        side_parts.append(clean_text(item.get("preview_text") or item.get("subject")))
        linked_text = clean_text(item.get("linked_ticket_description"))
        if linked_text:
            side_parts.append(linked_text)
    side_text = " | ".join(part for part in side_parts if part)
    if not side_text:
        raise RuntimeError("No usable side-conversation text was returned")
    prompt = """Return JSON only with exactly two fields: title and description.
Use only the side-conversation text below, including any linked target-ticket
content attached to the side conversation. Do not use or infer details from the
source Zendesk ticket. Create a clear manager-friendly title of 8-14 words and a
concise description of the issue, impact, relevant details, and requested
resolution. Do not copy greetings, URLs, login links, IDs, metadata, or entire
paragraphs. If the side conversation asks for a specific follow-up, include it.

Side-conversation text:
{side_text}
""".format(side_text=side_text[:12000])

    completed = subprocess.run(
        [CODEX, "exec", "--sandbox", "read-only", "--ephemeral", "--skip-git-repo-check", prompt],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=180,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr[-1000:] or "Codex execution failed")
    output = completed.stdout.strip()
    start = output.find("{")
    end = output.rfind("}")
    if start < 0 or end <= start:
        raise RuntimeError("Codex did not return JSON: " + output[-500:])
    return json.loads(output[start : end + 1])


def publish_to_sheet(results):
    """Send local Codex results to the Apps Script web-app bridge."""
    url = os.environ.get("APPS_SCRIPT_WEB_APP_URL", "").strip()
    if not url:
        print("APPS_SCRIPT_WEB_APP_URL is not set; keeping local files only.")
        return
    payload = {
        "secret": os.environ.get("APPS_SCRIPT_SHARED_SECRET", ""),
        "rows": results,
    }
    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=60) as response:
            body = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError) as error:
        print("Google Sheets publish skipped: " + str(error))
        print("Local Codex results were still saved to local_demo_results.json and local_demo_results.csv.")
        return
    if not body.get("ok"):
        raise RuntimeError("Apps Script bridge rejected the update: " + json.dumps(body))
    print("Published " + str(body.get("updated", 0)) + " rows to Google Sheets.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--hours", type=int, default=48)
    parser.add_argument("--start-date", default="", help="Only include side conversations submitted on/after YYYY-MM-DD")
    parser.add_argument("--limit", type=int, default=1)
    parser.add_argument("--sample", action="store_true", help="Run with synthetic data and no Zendesk request")
    args = parser.parse_args()
    if not args.sample:
        load_env()
        load_product_config()

    if args.sample:
        candidates = [{"id": "DEMO-001"}]
    elif args.start_date:
        candidates = search_tickets_created_since(args.start_date)
    else:
        candidates = search_recent_tickets(args.hours)
    results = []
    for search_result in candidates:
        if len(results) >= args.limit:
            break
        if args.sample:
            child_ticket = {"created_at": "2026-10-08T15:30:00Z"}
            ticket = {
                "id": "DEMO-001",
                "created_at": "2026-10-08T15:30:00Z",
                "assignee": {"name": "Demo Agent"},
                "description": "A user cannot access a product after a profile change. Investigate the access issue and restore normal use.",
            }
            product = "Example Product"
            conversations = [{"preview_text": "Please investigate the access issue and provide the requested resolution."}]
        else:
            child_ticket = get_ticket(search_result["id"])
            if is_external_escalation(child_ticket):
                continue
            parent_id = get_parent_ticket_id(child_ticket)
            ticket = get_ticket(parent_id) if parent_id else child_ticket
            if is_external_escalation(ticket):
                continue
            product = get_product(ticket)
            if not product:
                continue
            conversations = get_side_conversations(ticket["id"])
            if not conversations:
                continue
            side_conversation_date = get_side_conversation_date(
                conversations, child_ticket.get("created_at", "")
            )
            if args.start_date and side_conversation_date < str(args.start_date):
                continue
        ticket_data = {
            "date": (
                get_side_conversation_date(
                    conversations, child_ticket.get("created_at", "")
                )
                if not args.sample
                else child_ticket.get("created_at", "")[:10]
            ),
            "agent": ticket.get("assignee", {}).get("name", ""),
            "zendesk_id": ticket["id"],
            "product": product,
            "side_conversations": conversations,
        }
        try:
            ai = call_codex(ticket_data)
        except Exception as error:
            ai = {"title": "AI summary failed", "description": str(error)}
        jira_info = get_jira_info(ticket)
        results.append({
            "date": ticket_data["date"],
            "agent": ticket_data["agent"] or get_assignee_name(ticket),
            "zendesk_id": ticket_data["zendesk_id"],
            "title": clean_text(ai.get("title")),
            "description": clean_text(ai.get("description")),
            "status": clean_text(ticket.get("status")),
            "product": product,
            "lead_response": get_lead_response(ticket["id"], conversations),
            "jira_id": jira_info["jira_id"],
            "dev_response": jira_info["dev_response"],
            "jira_case_status": jira_info["jira_case_status"],
        })
        print("Processed ticket " + str(ticket_data["zendesk_id"]) + " (" + str(len(results)) + ")", flush=True)

    results.sort(key=lambda row: row["date"])
    with open(os.path.join(PROJECT_ROOT, "local_demo_results.json"), "w", encoding="utf-8") as handle:
        json.dump(results, handle, indent=2)
    with open(os.path.join(PROJECT_ROOT, "local_demo_results.csv"), "w", newline="", encoding="utf-8") as handle:
        fields = [
            "Date", "Agent", "Zendesk ID", "Title", "Description",
            "Zendesk Ticket status", "Product", "Lead Response", "Jira ID",
            "Dev Response", "Jira case status",
        ]
        writer = csv.writer(handle)
        writer.writerow(fields)
        for row in results:
            writer.writerow([
                row["date"],
                row["agent"],
                row["zendesk_id"],
                row["title"],
                row["description"],
                row["status"],
                row["product"],
                row["lead_response"],
                row["jira_id"],
                row["dev_response"],
                row["jira_case_status"],
            ])
    publish_to_sheet(results)
    print(json.dumps(results, indent=2))
    print("Saved local_demo_results.json and local_demo_results.csv")


if __name__ == "__main__":
    main()
