from __future__ import annotations

import json
from mcp.types import TextContent
import json
import urllib.parse
import urllib.request
from urllib.error import HTTPError, URLError

METADATA = {
    "name": "web_search",
    "description": "Search the web using SearXNG. Returns structured results with titles, URLs, and snippets from 70+ search engines. Use for any task requiring web research, fact-finding, or information discovery.",
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query string"
            },
            "max_results": {
                "type": "integer",
                "description": "Maximum number of results to return (default 10, max 30)",
                "default": 10
            },
            "categories": {
                "type": "string",
                "description": "Search category: general, news, images, science, files, social_media (default: general)",
                "default": "general"
            }
        },
        "required": [
            "query"
        ]
    }
}


async def handle(params: dict, db):
    query = (params.get("query") or "").strip()
    if not query:
        return {"error": "query is required", "query": "", "result_count": 0, "results": []}
    
    try:
        max_results = int(params.get("max_results", 10))
    except (TypeError, ValueError):
        max_results = 10
    max_results = max(1, min(max_results, 30))
    
    categories = (params.get("categories") or "general").strip() or "general"
    url = "http://localhost:8080/search?" + urllib.parse.urlencode({
        "q": query,
        "format": "json",
        "categories": categories,
    })
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "Custodian web_search/1.0",
        },
    )
    
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        return {
            "error": f"SearXNG HTTP error: {exc.code}",
            "query": query,
            "result_count": 0,
            "results": [],
        }
    except URLError as exc:
        return {
            "error": f"SearXNG connection error: {exc.reason}",
            "query": query,
            "result_count": 0,
            "results": [],
        }
    except Exception as exc:
        return {
            "error": f"SearXNG request failed: {exc}",
            "query": query,
            "result_count": 0,
            "results": [],
        }
    
    results = []
    for item in payload.get("results", [])[:max_results]:
        results.append({
            "title": item.get("title", ""),
            "url": item.get("url", ""),
            "snippet": item.get("content", ""),
            "engine": item.get("engine", ""),
        })
    
    response = {
        "query": query,
        "categories": categories,
        "result_count": len(results),
        "results": results,
    }
    
    if not results and payload.get("unresponsive_engines"):
        response["warning"] = "No results returned; some engines were unresponsive"
        response["unresponsive_engines"] = payload.get("unresponsive_engines", [])
    
    return response
