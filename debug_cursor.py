import urllib.parse
req = type('obj', (object,), {'full_url' : 'https://test.jules.api/sessions/123/activities'})
parsed = urllib.parse.urlparse(req.full_url)
qs = urllib.parse.parse_qs(parsed.query)
token = qs.get("pageToken", [None])[0] if "pageToken" in qs else None
print(token)
