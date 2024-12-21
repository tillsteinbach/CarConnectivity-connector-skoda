def add_bearer_auth_header(token, headers=None):
    headers = headers or {}
    headers['Authorization'] = f'Bearer {token}'
    return headers
