import io, json, os
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from fastapi.testclient import TestClient

VALID_SUMMARY = {
    "title": "Test Document",
    "summary": "This is a test summary.",
    "key_points": ["Point one", "Point two", "Point three"],
    "word_count": 100,
    "language": "en",
    "sentiment": "neutral",
    "topics": ["testing"],
}

MINIMAL_PDF = (
    b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
    b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n"
    b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792]\n"
    b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>\nendobj\n"
    b"4 0 obj\n<< /Length 44 >>\nstream\nBT /F1 12 Tf 100 700 Td (Hello) Tj ET\n"
    b"endstream\nendobj\n5 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n"
    b"xref\n0 6\n0000000000 65535 f \n0000000009 00000 n \n0000000058 00000 n \n"
    b"0000000115 00000 n \n0000000266 00000 n \n0000000360 00000 n \n"
    b"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n441\n%%EOF\n"
)


def make_mock_claude(content):
    msg = MagicMock()
    msg.content = [MagicMock(text=json.dumps(content))]
    msg.usage.input_tokens = 500
    msg.usage.output_tokens = 120
    msg.model = "claude-sonnet-4-6-20250514"
    return msg


@pytest.fixture()
def client():
    with patch("main.client") as mock_anthropic:
        mock_anthropic.messages.create.return_value = make_mock_claude(VALID_SUMMARY)
        from main import app
        yield TestClient(app), mock_anthropic


class TestHealth:
    def test_returns_200(self, client):
        tc, _ = client
        assert tc.get("/health").status_code == 200

    def test_returns_ok(self, client):
        tc, _ = client
        assert tc.get("/health").json() == {"status": "ok"}


class TestAgentSpec:
    def test_agent_spec_returns_200(self, client):
        tc, _ = client
        assert tc.get("/agent-spec.md").status_code == 200

    def test_agent_spec_contains_price(self, client):
        tc, _ = client
        assert "50 satoshis" in tc.get("/agent-spec.md").text

    def test_agent_spec_contains_endpoints(self, client):
        tc, _ = client
        text = tc.get("/agent-spec.md").text
        assert "/summarise/upload" in text
        assert "/summarise/url" in text

    def test_agent_spec_contains_l402(self, client):
        tc, _ = client
        assert "L402" in tc.get("/agent-spec.md").text

    def test_well_known_returns_200(self, client):
        tc, _ = client
        assert tc.get("/.well-known/l402.json").status_code == 200

    def test_well_known_has_pricing(self, client):
        tc, _ = client
        assert "pricing" in tc.get("/.well-known/l402.json").json()

    def test_well_known_protocol_is_l402(self, client):
        tc, _ = client
        body = tc.get("/.well-known/l402.json").json()
        assert body["payment"]["protocol"] == "L402"


class TestUpload:
    def _upload(self, tc, content=MINIMAL_PDF, filename="test.pdf"):
        return tc.post(
            "/summarise/upload",
            files={"file": (filename, io.BytesIO(content), "application/pdf")},
        )

    def test_returns_200(self, client):
        tc, _ = client
        with patch("main.extract_text_from_pdf_bytes", return_value="Some text"):
            assert self._upload(tc).status_code == 200

    def test_has_required_fields(self, client):
        tc, _ = client
        with patch("main.extract_text_from_pdf_bytes", return_value="Some text"):
            body = self._upload(tc).json()
        for f in ["title", "summary", "key_points", "word_count",
                  "language", "sentiment", "topics", "_meta"]:
            assert f in body

    def test_rejects_non_pdf(self, client):
        tc, _ = client
        assert self._upload(tc, filename="doc.txt").status_code == 415

    def test_rejects_oversized(self, client):
        tc, _ = client
        big = b"%PDF-1.4\n" + b"x" * (10 * 1024 * 1024 + 1)
        assert self._upload(tc, content=big).status_code == 413

    def test_returns_500_on_bad_json(self, client):
        tc, mock_claude = client
        bad = MagicMock()
        bad.content = [MagicMock(text="not json")]
        bad.usage.input_tokens = 100
        bad.usage.output_tokens = 10
        bad.model = "claude-sonnet-4-6-20250514"
        mock_claude.messages.create.return_value = bad
        with patch("main.extract_text_from_pdf_bytes", return_value="text"):
            assert self._upload(tc).status_code == 500

    def test_uses_correct_model(self, client):
        tc, mock_claude = client
        with patch("main.extract_text_from_pdf_bytes", return_value="text"):
            self._upload(tc)
        assert mock_claude.messages.create.call_args.kwargs["model"] == "claude-sonnet-4-6"

    def test_strips_markdown_fences(self, client):
        tc, mock_claude = client
        wrapped = MagicMock()
        wrapped.content = [MagicMock(
            text="```json\n" + json.dumps(VALID_SUMMARY) + "\n```"
        )]
        wrapped.usage.input_tokens = 100
        wrapped.usage.output_tokens = 20
        wrapped.model = "claude-sonnet-4-6-20250514"
        mock_claude.messages.create.return_value = wrapped
        with patch("main.extract_text_from_pdf_bytes", return_value="text"):
            assert self._upload(tc).status_code == 200


class TestUrl:
    def _mock_http(self):
        mock_response = MagicMock()
        mock_response.content = MINIMAL_PDF
        mock_response.headers = {"content-type": "application/pdf"}
        mock_response.raise_for_status = MagicMock()
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_response)
        return mock_client

    def test_returns_200(self, client):
        tc, _ = client
        with patch("httpx.AsyncClient", return_value=self._mock_http()):
            with patch("main.extract_text_from_pdf_bytes", return_value="text"):
                r = tc.post("/summarise/url", json={"url": "https://example.com/doc.pdf"})
        assert r.status_code == 200

    def test_rejects_missing_url(self, client):
        tc, _ = client
        assert tc.post("/summarise/url", json={}).status_code == 422

    def test_returns_400_on_fetch_failure(self, client):
        import httpx as real_httpx
        tc, _ = client
        mock_http = AsyncMock()
        mock_http.__aenter__ = AsyncMock(return_value=mock_http)
        mock_http.__aexit__ = AsyncMock(return_value=False)
        mock_http.get = AsyncMock(side_effect=real_httpx.HTTPError("refused"))
        with patch("httpx.AsyncClient", return_value=mock_http):
            r = tc.post("/summarise/url", json={"url": "https://example.com/doc.pdf"})
        assert r.status_code == 400


class TestExtractText:
    def test_extracts_text(self):
        from main import extract_text_from_pdf_bytes
        p = MagicMock()
        p.extract_text.return_value = "Hello world"
        pdf = MagicMock()
        pdf.__enter__ = MagicMock(return_value=pdf)
        pdf.__exit__ = MagicMock(return_value=False)
        pdf.pages = [p]
        with patch("pdfplumber.open", return_value=pdf):
            assert "Hello world" in extract_text_from_pdf_bytes(b"fake")

    def test_raises_422_on_empty(self):
        from fastapi import HTTPException
        from main import extract_text_from_pdf_bytes
        p = MagicMock()
        p.extract_text.return_value = None
        pdf = MagicMock()
        pdf.__enter__ = MagicMock(return_value=pdf)
        pdf.__exit__ = MagicMock(return_value=False)
        pdf.pages = [p]
        with patch("pdfplumber.open", return_value=pdf):
            with pytest.raises(HTTPException) as exc:
                extract_text_from_pdf_bytes(b"fake")
        assert exc.value.status_code == 422
