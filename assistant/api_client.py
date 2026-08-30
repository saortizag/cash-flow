"""A thin HTTP client for the real Cash REST API — the same API any other consumer (a script,
curl, a future WhatsApp webhook) would use. Deliberately dumb: no business logic lives here, no
decisions about whether a transaction is "allowed." All of that already lives server-side in
ledger/services.py (locking, validation, balance updates) — this client just does auth + HTTP.
That's also why nothing here imports Django: this whole assistant/ directory could run on a
different machine than the Cash server.
"""
import requests


class CashApiError(RuntimeError):
    """Wraps a non-2xx response so callers see the server's own error detail (e.g.
    {"amount": ["A valid number is required."]}) instead of a bare requests.HTTPError."""


class CashApiClient:
    def __init__(self, base_url: str, username: str, password: str):
        self.base_url = base_url.rstrip('/')
        self._username = username
        self._password = password
        self._access_token = None
        self._refresh_token = None

    # ---------- auth ----------
    # Same two-step JWT dance any browser or script would do against /api/token/ and
    # /api/token/refresh/ — see api/urls.py. Tokens are cached in memory only; a longer-lived
    # process (like the future WhatsApp webhook) would hit this same client instance repeatedly
    # and only re-authenticate when a token actually expires.

    def _login(self):
        response = requests.post(f'{self.base_url}/api/token/',
                                  json={'username': self._username, 'password': self._password})
        response.raise_for_status()
        data = response.json()
        self._access_token = data['access']
        self._refresh_token = data['refresh']

    def _refresh(self):
        response = requests.post(f'{self.base_url}/api/token/refresh/', json={'refresh': self._refresh_token})
        if response.status_code != 200:
            # Refresh token itself expired (14-day lifetime) or was never obtained — start clean.
            self._login()
            return
        self._access_token = response.json()['access']

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        if self._access_token is None:
            self._login()
        url = f'{self.base_url}{path}'
        headers = {'Authorization': f'Bearer {self._access_token}'}
        response = requests.request(method, url, headers=headers, **kwargs)
        if response.status_code == 401:
            # Access token expired mid-session (1hr lifetime) — refresh once and retry once.
            # A second 401 after that is a real auth problem, not an expiry, so it falls through
            # to the raise below instead of looping.
            self._refresh()
            headers['Authorization'] = f'Bearer {self._access_token}'
            response = requests.request(method, url, headers=headers, **kwargs)
        if not response.ok:
            raise CashApiError(f'{method} {path} -> {response.status_code}: {response.text}')
        return response

    # ---------- reads ----------
    # .../accounts/ and .../categories/ are paginated (PAGE_SIZE=50, see cash/settings.py) but a
    # personal finance tool realistically never has more than a handful of either, so a single
    # page is assumed rather than following `next` — fine here, would need revisiting if this
    # client were reused somewhere with hundreds of accounts.

    def list_accounts(self) -> list[dict]:
        return self._request('GET', '/api/v1/accounts/').json()['results']

    def list_categories(self) -> list[dict]:
        return self._request('GET', '/api/v1/categories/').json()['results']

    # ---------- writes ----------

    def create_transaction(self, *, account_id: int, category_id: int, direction: str,
                            amount: float, description: str, executed: bool = True,
                            executed_date=None) -> dict:
        """due_date is intentionally omitted: the server only requires it for a NOT-yet-executed
        transaction (see api/serializers.py's TransactionCreateSerializer due_date comment) — this
        assistant always records executed=True, already-happened purchases (see agent.py), so
        it's never needed here."""
        payload = {
            'account': account_id,
            'category': category_id,
            'direction': direction,
            'amount': str(amount),  # string, not float, to avoid binary-float round-tripping through JSON
            'description': description,
            'executed': executed,
        }
        if executed_date is not None:
            payload['executed_date'] = executed_date.isoformat()
        return self._request('POST', '/api/v1/transactions/', json=payload).json()
