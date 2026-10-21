"""Call-local main-document response receipts for rendered source admission.

Entry: DocumentResponse, scoped to one page's existing navigation/read lifetime.
Responses from subframes and ordinary resources cannot authorize page content.
The pool owns navigation/timeouts; this observer adds no wait or request.
"""


class DocumentResponse:
    def __init__(self, page):
        self.page = page
        self.status = None
        self.generation = 0
        self._request = None
        self._request_listener = self._started
        self._response_listener = self._received

    def _started(self, request):
        if request.is_navigation_request() and request.frame == self.page.main_frame:
            self._request = request
            self.status = None
            self.generation += 1

    def _received(self, response):
        if response.request is self._request:
            self.status = response.status
            self.generation += 1

    @property
    def allows_source(self):
        return isinstance(self.status, int) and 200 <= self.status < 300

    def __enter__(self):
        self.page.on('request', self._request_listener)
        try:
            self.page.on('response', self._response_listener)
        except BaseException:
            self.page.remove_listener('request', self._request_listener)
            raise
        return self

    def __exit__(self, *_args):
        try:
            self.page.remove_listener('response', self._response_listener)
        finally:
            self.page.remove_listener('request', self._request_listener)
