"""Check public football and market data from the current resolver namespace."""

import json
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener


ENDPOINTS = (
    ("gamma", "https://gamma-api.polymarket.com/events?tag_id=306&limit=1"),
    ("clob", "https://clob.polymarket.com/time"),
    ("data", "https://data-api.polymarket.com/v2/trades?limit=1"),
    ("wikipedia", "https://en.wikipedia.org/w/api.php?action=query&meta=siteinfo&format=json"),
    ("wikimedia", "https://api.wikimedia.org/core/v1/wikipedia/en/page/Chelsea_F.C."),
    ("openligadb", "https://api.openligadb.de/getlastchangedate/pl/2026/12"),
    ("touchline", "https://touchlinewire.com/robots.txt"),
)


def check(opener, label, url):
    status = None
    content_type = None
    try:
        request = Request(
            url, headers={"User-Agent": "polymarket861/0.5.0a3 (public endpoint check)"}
        )
        with opener.open(request, timeout=8) as response:
            status = response.status
            content_type = response.headers.get_content_type()
            body = response.read()
        if label == "touchline" or (label == "openligadb" and content_type == "text/plain"):
            value = body.decode("utf-8")
        else:
            value = json.loads(body)
        return {
            "endpoint": label,
            "http_status": status,
            "content_type": content_type,
            "response_type": type(value).__name__,
        }
    except HTTPError as error:
        status = error.code
        reason = error.reason
        error.close()
    except (OSError, ValueError) as error:
        reason = str(error)
    return {
        "endpoint": label,
        "http_status": status,
        "content_type": content_type,
        "response_type": None,
        "error": str(reason),
    }


def main():
    opener = build_opener(ProxyHandler({}))
    results = [check(opener, label, url) for label, url in ENDPOINTS]
    for result in results:
        print(json.dumps(result, ensure_ascii=False))
    return 0 if all(
        result["http_status"] == 200 and "error" not in result for result in results
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
