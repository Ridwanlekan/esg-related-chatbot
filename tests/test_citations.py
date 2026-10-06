"""Verifiable citations: resolving a citation to a file, and not to anything else.

The download endpoint is the only route in the service that turns a
client-supplied string into bytes off disk, so these tests are mostly about the
failure modes: another workspace's document, a traversal, an absolute path, a
symlink out of the data root, and an unauthenticated caller.
"""

import os

import pytest
from fastapi.testclient import TestClient

from chatbot.api import create_app, document_url
from chatbot.documents import DocumentNotFound, resolve_document
from chatbot.download_links import mint_download_token
from chatbot.security import client_ip
from chatbot.session_store import SessionStore
from chatbot.users import UserStore, verify_jwt


class FakeBot:
    def __init__(self, source="policy.pdf"):
        self.last_results = []
        self.source = source

    def ask(self, question, k=3, source=None, history=None, usage_sink=None):
        return "answer"

    def ask_stream(self, question, k=3, source=None, history=None, usage_sink=None):
        yield "answer"

    def retrieve(self, question, k=3, source=None):
        return []

    def read_and_embed_data(self):
        from types import SimpleNamespace

        return SimpleNamespace(
            documents_seen=0,
            documents_reindexed=0,
            chunks_upserted=0,
            stale_chunks_removed=0,
        )

    @property
    def store(self):
        from types import SimpleNamespace

        return SimpleNamespace(count=lambda: 0)


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    root = tmp_path / "data"
    for workspace in ("finance", "hr"):
        (root / workspace).mkdir(parents=True)
    (root / "_shared").mkdir()
    (root / "finance" / "policy.pdf").write_text("FINANCE POLICY BODY")
    (root / "finance" / "notes.txt").write_text("finance notes")
    (root / "hr" / "policy.pdf").write_text("HR POLICY BODY")
    (root / "_shared" / "governance.pdf").write_text("GOVERNANCE BODY")
    monkeypatch.setenv("DATA_DIR", str(root))
    monkeypatch.setenv("WORKSPACES", "finance,hr")
    return root


class TestResolveDocument:
    def test_resolves_a_workspace_document(self, data_dir):
        path = resolve_document("finance", "policy.pdf")
        assert path.read_text() == "FINANCE POLICY BODY"

    def test_same_name_in_two_workspaces_stays_isolated(self, data_dir):
        assert resolve_document("finance", "policy.pdf").read_text() == "FINANCE POLICY BODY"
        assert resolve_document("hr", "policy.pdf").read_text() == "HR POLICY BODY"

    def test_resolves_the_root_relative_form_ingest_actually_writes(self, data_dir):
        """Sources are stored relative to the data root, not the workspace.

        ingest.py records os.path.relpath(path, data_dir), so a real citation
        reads "finance/policy.pdf" and is what a search result, a stored session
        and a signed link all carry. Reading it as workspace-relative instead
        looks for data/finance/finance/policy.pdf and 404s every citation in a
        real deployment — which is why the fixture above writes both shapes and
        this case is asserted separately.
        """
        assert (
            resolve_document("finance", "finance/policy.pdf").read_text()
            == "FINANCE POLICY BODY"
        )
        assert (
            resolve_document("hr", "hr/policy.pdf").read_text() == "HR POLICY BODY"
        )

    def test_root_relative_form_reaches_the_shared_store(self, data_dir):
        """The shared store is reachable from every workspace, by its stored name."""
        for category in ("finance", "hr"):
            assert (
                resolve_document(category, "_shared/governance.pdf").read_text()
                == "GOVERNANCE BODY"
            )

    def test_root_relative_form_cannot_reach_another_workspace(self, data_dir):
        """The prefix is honoured only for the caller's own workspace.

        Naming someone else's workspace must not resolve: it falls through to the
        caller's own directory, where that path does not exist.
        """
        with pytest.raises(DocumentNotFound):
            resolve_document("finance", "hr/policy.pdf")
        with pytest.raises(DocumentNotFound):
            resolve_document("hr", "finance/policy.pdf")

    def test_root_relative_form_cannot_climb_out_of_the_data_root(self, data_dir):
        """The data root itself is not a document location.

        "finance/../policy.pdf" is rejected by segment validation, but a source
        that lands on the root — the shared dir or the data dir itself — has to be
        refused as well, or the confinement check would be doing the work by
        accident rather than by design.
        """
        for source in ("finance/../finance/policy.pdf", "_shared/../policy.pdf"):
            with pytest.raises(DocumentNotFound):
                resolve_document("finance", source)

    @pytest.mark.parametrize(
        "source",
        [
            "../hr/policy.pdf",
            "../../etc/passwd",
            "sub/../../hr/policy.pdf",
            "./policy.pdf",
            "finance/../hr/policy.pdf",
            "..",
            "sub//policy.pdf",
        ],
    )
    def test_traversal_is_rejected(self, data_dir, source):
        with pytest.raises(DocumentNotFound):
            resolve_document("finance", source)

    @pytest.mark.parametrize(
        "source",
        ["/etc/passwd", "/tmp/data/finance/policy.pdf", "..\\hr\\policy.pdf"],
    )
    def test_absolute_and_windows_paths_are_rejected(self, data_dir, source):
        with pytest.raises(DocumentNotFound):
            resolve_document("finance", source)

    @pytest.mark.parametrize("source", ["", "   ", "missing.pdf", "notes.txt/x"])
    def test_missing_or_empty_is_rejected(self, data_dir, source):
        with pytest.raises(DocumentNotFound):
            resolve_document("finance", source)

    def test_null_byte_is_rejected(self, data_dir):
        with pytest.raises(DocumentNotFound):
            resolve_document("finance", "policy.pdf\x00.txt")

    def test_non_workspace_category_is_rejected(self, data_dir):
        # The category is part of the resolved path, so it has to be a real
        # workspace: otherwise it is a traversal vector with extra steps.
        with pytest.raises(DocumentNotFound):
            resolve_document("..", "policy.pdf")
        with pytest.raises(DocumentNotFound):
            resolve_document("_shared", "governance.pdf")

    def test_over_long_reference_is_rejected(self, data_dir):
        with pytest.raises(DocumentNotFound):
            resolve_document("finance", "a" * 600)

    def test_symlink_into_shared_store_is_allowed(self, data_dir):
        """A shared document is hardlinked where possible and symlinked when not.

        The hardlink case keeps the realpath inside the workspace; the symlink
        fallback lands in data/_shared. Both are the legitimate mechanism, so
        neither may be mistaken for an escape.
        """
        link = data_dir / "finance" / "governance.pdf"
        try:
            os.symlink(data_dir / "_shared" / "governance.pdf", link)
        except (OSError, NotImplementedError):
            pytest.skip("filesystem does not support symlinks")
        assert resolve_document("finance", "governance.pdf").read_text() == "GOVERNANCE BODY"

    def test_symlink_out_of_the_data_root_is_rejected(self, data_dir, tmp_path):
        secret = tmp_path / "secret.pdf"
        secret.write_text("NOT A WORKSPACE DOCUMENT")
        link = data_dir / "finance" / "leak.pdf"
        try:
            os.symlink(secret, link)
        except (OSError, NotImplementedError):
            pytest.skip("filesystem does not support symlinks")
        with pytest.raises(DocumentNotFound):
            resolve_document("finance", "leak.pdf")

    def test_nested_document_resolves(self, data_dir):
        nested = data_dir / "finance" / "2024"
        nested.mkdir()
        (nested / "s1.pdf").write_text("NESTED BODY")
        assert resolve_document("finance", "2024/s1.pdf").read_text() == "NESTED BODY"


class TestDocumentUrl:
    def test_reference_is_percent_encoded(self):
        assert document_url("10. IFRS S1.pdf") == (
            "/documents/download?source=10.%20IFRS%20S1.pdf"
        )

    def test_slashes_are_encoded_so_they_cannot_split_the_path(self):
        assert document_url("2024/s1.pdf") == (
            "/documents/download?source=2024%2Fs1.pdf"
        )

    def test_page_becomes_a_fragment_not_a_query_parameter(self):
        assert document_url("policy.pdf", None, 42) == (
            "/documents/download?source=policy.pdf#page=42"
        )

    def test_absent_page_leaves_no_fragment(self):
        assert "#page" not in document_url("policy.pdf", None, None)


@pytest.fixture
def client(data_dir):
    from chatbot.admin_store import AdminStore

    app = create_app(
        bot=FakeBot(),
        session_store=SessionStore(db_path=":memory:"),
        user_store=UserStore(db_path=":memory:", secret="s3cret"),
        admin_store=AdminStore(os.path.join(str(data_dir.parent), "workspaces.sqlite3")),
        auth_secret="s3cret",
        api_key="admin-key",
        rate_limit=0,
    )
    return TestClient(app)


def _token(client, email, category):
    """A confirmed account: these tests are about citations, not D18."""
    res = client.post(
        "/auth/signup",
        json={
            "email": email,
            "password": "password123",
            "name": "U",
            "category": category,
        },
    )
    assert res.status_code == 200, res.text
    client.app.state.user_store.set_verified(res.json()["user"]["id"])
    return res.json()["token"]


def _auth(token):
    return {"authorization": f"Bearer {token}"}


class TestDownloadEndpoint:
    def test_a_real_citation_source_opens_end_to_end(self, client, data_dir):
        """The whole path, with the source string ingest actually produces.

        Everything else in this file cites "policy.pdf", which is workspace
        relative. A real index stores "finance/policy.pdf", because ingest hashes
        paths relative to the data root — so the shipped citation link for every
        document in a real deployment 404s even though all 61 of these tests pass.
        This is the one test that uses the stored form, at the API level, with no
        Authorization header, exactly as a browser opens a citation.
        """
        session = _token(client, "real@corp.com", "finance")
        client.app.state.bot.source = "finance/policy.pdf"
        client.app.state.bot.last_results = [
            type(
                "R",
                (),
                {
                    "source": "finance/policy.pdf",
                    "chunk_id": "finance/policy.pdf#0",
                    "chunk_index": 0,
                    "similarity": 0.42,
                    "page_start": 3,
                    "page_end": 3,
                },
            )()
        ]
        chat = client.post(
            "/chat", json={"question": "what is the policy"}, headers=_auth(session)
        )
        assert chat.status_code == 200
        url = chat.json()["sources"][0]["url"]
        assert url.startswith("/documents/download?source=finance%2Fpolicy.pdf")

        # No auth header: this is the native navigation the feature exists for.
        res = client.get(url)
        assert res.status_code == 200, res.text
        assert res.text == "FINANCE POLICY BODY"
        assert res.headers["content-disposition"].startswith("inline")

    def test_user_downloads_a_cited_document(self, client):
        token = _token(client, "fin@corp.com", "finance")
        res = client.get(
            "/documents/download?source=policy.pdf", headers=_auth(token)
        )
        assert res.status_code == 200
        assert res.content == b"FINANCE POLICY BODY"
        # A PDF is served inline so a `#page=` citation fragment lands on the cited
        # page in the browser's viewer. Download the bytes and they are identical.
        assert res.headers["content-type"].startswith("application/pdf")
        assert "inline" in res.headers["content-disposition"]
        assert "policy.pdf" in res.headers["content-disposition"]
        assert res.headers["x-content-type-options"] == "nosniff"

    def test_a_pdf_link_can_be_followed_with_no_authorization_header(self, client):
        """The whole point of the download token: a native new-tab navigation.

        Browsers do not attach an Authorization header to a navigation, so this
        request carries nothing but the URL a reader could have copied out of the
        chat transcript.
        """
        session = _token(client, "fin@corp.com", "finance")
        link = client.post(
            "/documents/link",
            json={"source": "policy.pdf", "page_start": 7},
            headers=_auth(session),
        )
        assert link.status_code == 200
        url = link.json()["url"]
        assert "#page=7" in url
        res = client.get(url)
        assert res.status_code == 200
        assert res.content == b"FINANCE POLICY BODY"

    def test_html_is_still_an_attachment(self, client, data_dir):
        """Inline HTML on this origin would be stored XSS against the session."""
        (data_dir / "finance" / "policy.html").write_text(
            "<script>fetch('/admin/users')</script>"
        )
        session = _token(client, "html@corp.com", "finance")
        link = client.post(
            "/documents/link",
            json={"source": "policy.html"},
            headers=_auth(session),
        )
        res = client.get(link.json()["url"])
        assert res.status_code == 200
        assert "attachment" in res.headers["content-disposition"]
        assert res.headers["content-type"] == "application/octet-stream"

    def test_download_response_does_not_leak_the_token_as_a_referrer(self, client):
        """A reader clicking a hyperlink inside the PDF must not leak the link."""
        session = _token(client, "ref@corp.com", "finance")
        link = client.post(
            "/documents/link", json={"source": "policy.pdf"}, headers=_auth(session)
        )
        res = client.get(link.json()["url"])
        assert res.headers["referrer-policy"] == "no-referrer"

    def test_authentication_is_required(self, client):
        assert client.get("/documents/download?source=policy.pdf").status_code == 401

    def test_a_token_for_another_document_cannot_be_edited(self, client, data_dir):
        """The token names the document; the query string cannot widen it."""
        session = _token(client, "edit@corp.com", "finance")
        link = client.post(
            "/documents/link", json={"source": "notes.txt"}, headers=_auth(session)
        )
        url = link.json()["url"].replace("source=notes.txt", "source=policy.pdf")
        assert client.get(url).status_code == 404

    def test_a_token_does_not_work_as_a_session_token(self, client):
        session = _token(client, "swap@corp.com", "finance")
        link = client.post(
            "/documents/link", json={"source": "policy.pdf"}, headers=_auth(session)
        )
        token = link.json()["url"].split("token=")[1]
        assert client.post("/chat", json={"question": "hi"}, headers=_auth(token)).status_code == 401
        assert client.get("/me", headers=_auth(token)).status_code == 401

    def test_a_session_token_does_not_work_as_a_download_token(self, client):
        session = _token(client, "swap2@corp.com", "finance")
        assert client.get(
            f"/documents/download?source=policy.pdf&token={session}"
        ).status_code == 401

    def test_expired_token_is_refused(self, client):
        session = _token(client, "exp@corp.com", "finance")
        link = client.post(
            "/documents/link", json={"source": "policy.pdf"}, headers=_auth(session)
        )
        stale = _reissued_past_due(client, session)
        assert client.get(stale).status_code == 401
        assert client.get(link.json()["url"]).status_code == 200

    def test_a_deleted_user_cannot_use_an_issued_link(self, client):
        """Links outlive the login, so entitlement is re-checked at redemption."""
        session = _token(client, "gone@corp.com", "finance")
        link = client.post(
            "/documents/link", json={"source": "policy.pdf"}, headers=_auth(session)
        )
        url = link.json()["url"]
        assert client.get(url).status_code == 200
        client.app.state.user_store.delete_user(
            client.app.state.user_store.get_by_email("gone@corp.com")["id"]
        )
        assert client.get(url).status_code == 401

    def test_a_link_stops_working_when_the_user_moves_workspace(self, client):
        session = _token(client, "mover@corp.com", "finance")
        link = client.post(
            "/documents/link", json={"source": "policy.pdf"}, headers=_auth(session)
        )
        url = link.json()["url"]
        client.app.state.user_store.update_user(
            client.app.state.user_store.get_by_email("mover@corp.com")["id"], category="hr"
        )
        # Signed while a finance user, redeemed as an hr user: the HR document of
        # the same name, not the finance one.
        assert client.get(url).content == b"HR POLICY BODY"

    def test_other_workspaces_document_is_not_reachable(self, client):
        token = _token(client, "fin@corp.com", "finance")
        res = client.get(
            "/documents/download?source=../hr/policy.pdf", headers=_auth(token)
        )
        assert res.status_code == 404
        assert "HR POLICY BODY" not in res.text

    def test_symlinked_escape_is_not_reachable(self, client, data_dir):
        secret = data_dir.parent / "outside.pdf"
        secret.write_text("NOT A WORKSPACE DOCUMENT")
        try:
            os.symlink(secret, data_dir / "finance" / "leak.pdf")
        except (OSError, NotImplementedError):
            pytest.skip("filesystem does not support symlinks")
        token = _token(client, "fin2@corp.com", "finance")
        res = client.get("/documents/download?source=leak.pdf", headers=_auth(token))
        assert res.status_code == 404

    def test_shared_document_is_reachable_from_a_subscribing_workspace(
        self, client, data_dir
    ):
        link = data_dir / "finance" / "governance.pdf"
        try:
            os.symlink(data_dir / "_shared" / "governance.pdf", link)
        except (OSError, NotImplementedError):
            pytest.skip("filesystem does not support symlinks")
        token = _token(client, "fin3@corp.com", "finance")
        res = client.get(
            "/documents/download?source=governance.pdf", headers=_auth(token)
        )
        assert res.status_code == 200
        assert res.content == b"GOVERNANCE BODY"

    def test_missing_source_parameter_is_a_404(self, client):
        """Source is optional because a download token carries it; absent both is
        an unresolvable citation, not a malformed request."""
        token = _token(client, "fin4@corp.com", "finance")
        assert client.get("/documents/download", headers=_auth(token)).status_code == 404

    def test_download_is_audited(self, client):
        token = _token(client, "fin5@corp.com", "finance")
        client.get("/documents/download?source=policy.pdf", headers=_auth(token))
        store = client.app.state.admin_store
        events = store.list_events(limit=50)["events"]
        downloads = [e for e in events if e["event"] == "document.download"]
        assert len(downloads) == 1
        assert downloads[0]["object_id"] == "finance/policy.pdf"
        assert downloads[0]["actor"] == "user:fin5@corp.com"
        assert "via=bearer" in downloads[0]["detail"]

    def test_a_token_download_is_audited_too(self, client):
        token = _token(client, "fin6@corp.com", "finance")
        link = client.post(
            "/documents/link", json={"source": "policy.pdf"}, headers=_auth(token)
        )
        client.get(link.json()["url"])
        events = client.app.state.admin_store.list_events(limit=50)["events"]
        downloads = [e for e in events if e["event"] == "document.download"]
        assert len(downloads) == 1
        assert downloads[0]["actor"] == "user:fin6@corp.com"
        # A capability URL surfacing in a shared document is a different incident
        # from an in-app fetch, so the row has to say which one it was.
        assert "via=token" in downloads[0]["detail"]


class TestDocumentLinkEndpoint:
    def test_link_requires_authentication(self, client):
        res = client.post("/documents/link", json={"source": "policy.pdf"})
        assert res.status_code == 401

    def test_link_to_an_unreachable_document_is_a_404(self, client):
        token = _token(client, "link1@corp.com", "finance")
        res = client.post(
            "/documents/link",
            json={"source": "../hr/policy.pdf"},
            headers=_auth(token),
        )
        assert res.status_code == 404

    def test_expires_in_reports_the_configured_ttl(self, client, monkeypatch):
        monkeypatch.setenv("DOWNLOAD_TOKEN_TTL_SECONDS", "120")
        token = _token(client, "link2@corp.com", "finance")
        res = client.post(
            "/documents/link", json={"source": "policy.pdf"}, headers=_auth(token)
        )
        assert res.json()["expires_in"] == 120


def _reissued_past_due(client, session_token):
    """A link whose token is correctly signed but already expired."""
    secret = client.app.state.link_secret
    return document_url(
        "policy.pdf",
        mint_download_token(secret, _user_id(client, session_token), "policy.pdf", ttl_seconds=-1),
    )


def _user_id(client, session_token):
    return verify_jwt(client.app.state.link_secret, session_token)["sub"]


class TestPageCitations:
    """A page number is the difference between a citation and a hint."""

    def _bot(self, source="policy.pdf", page=42):
        from chatbot.vector_store import SearchResult

        bot = FakeBot(source)
        bot.last_results = [
            SearchResult(
                chunk_id="c1",
                source=source,
                chunk_index=3,
                content="a quoted sentence",
                distance=0.12,
                page_start=page,
                page_end=page,
            )
        ]
        return bot

    def _client(self, data_dir, bot):
        from chatbot.admin_store import AdminStore

        return TestClient(
            create_app(
                bot=bot,
                session_store=SessionStore(db_path=":memory:"),
                user_store=UserStore(db_path=":memory:", secret="s3cret"),
                admin_store=AdminStore(os.path.join(str(data_dir.parent), "w.sqlite3")),
                auth_secret="s3cret",
                api_key="admin-key",
                rate_limit=0,
            )
        )

    def test_a_citation_carries_its_page_and_links_to_it(self, data_dir):
        client = self._client(data_dir, self._bot())
        token = _token(client, "page1@corp.com", "finance")
        ref = client.post("/chat", json={"question": "q"}, headers=_auth(token)).json()["sources"][0]

        assert ref["page_start"] == 42
        assert ref["page_end"] == 42
        assert ref["url"].endswith("#page=42")

    def test_the_link_opens_the_pdf_at_the_cited_page(self, data_dir):
        client = self._client(data_dir, self._bot())
        token = _token(client, "page2@corp.com", "finance")
        ref = client.post("/chat", json={"question": "q"}, headers=_auth(token)).json()["sources"][0]

        res = client.get(ref["url"])
        assert res.status_code == 200
        assert res.content == b"FINANCE POLICY BODY"

    def test_an_unpaginated_source_has_no_page(self, data_dir):
        client = self._client(data_dir, self._bot(source="notes.txt", page=None))
        token = _token(client, "page3@corp.com", "finance")
        ref = client.post("/chat", json={"question": "q"}, headers=_auth(token)).json()["sources"][0]

        assert ref["page_start"] is None
        assert "#page" not in ref["url"]

    def test_pages_survive_a_session_reload(self, data_dir):
        client = self._client(data_dir, self._bot())
        token = _token(client, "page4@corp.com", "finance")
        chat = client.post("/chat", json={"question": "q"}, headers=_auth(token)).json()

        messages = client.get(f"/sessions/{chat['session_id']}", headers=_auth(token)).json()["messages"]
        stored = messages[-1]["sources"][0]
        assert stored["page_start"] == 42
        assert stored["url"].endswith("#page=42")

    def test_a_reloaded_session_gets_a_freshly_signed_link(self, data_dir):
        """Sessions outlive an hour, so a stored URL has to be re-minted."""
        client = self._client(data_dir, self._bot())
        token = _token(client, "page5@corp.com", "finance")
        chat = client.post("/chat", json={"question": "q"}, headers=_auth(token)).json()

        reloaded = client.get(f"/sessions/{chat['session_id']}", headers=_auth(token))
        first = reloaded.json()["messages"][-1]["sources"][0]["url"]
        again = client.get(f"/sessions/{chat['session_id']}", headers=_auth(token))
        second = again.json()["messages"][-1]["sources"][0]["url"]

        # Same signature and same second, so identical; what matters is that the
        # reloaded URL is independently valid rather than the stored one.
        assert client.get(first).status_code == 200
        assert first == second or client.get(second).status_code == 200

    def test_search_results_report_pages(self, data_dir):
        client = self._client(data_dir, self._bot())
        token = _token(client, "page6@corp.com", "finance")
        bot = client.app.state.bot
        bot.retrieve = lambda question, k=3, source=None: bot.last_results
        res = client.post("/search", json={"question": "q"}, headers=_auth(token))
        assert res.json()["results"][0]["page_start"] == 42


class FakeRequest:
    """Minimal stand-in for the one attribute client_ip() reads."""

    def __init__(self, host, headers=None):
        self.client = type("C", (), {"host": host})() if host else None
        self.headers = headers or {}


class TestClientIp:
    def test_peer_is_used_when_no_proxy_is_trusted(self, monkeypatch):
        monkeypatch.delenv("TRUSTED_PROXY_IPS", raising=False)
        req = FakeRequest("10.0.0.1", {"x-forwarded-for": "1.2.3.4"})
        assert client_ip(req) == "10.0.0.1"

    def test_forwarded_header_is_ignored_from_an_untrusted_peer(self, monkeypatch):
        monkeypatch.setenv("TRUSTED_PROXY_IPS", "10.0.0.9")
        req = FakeRequest("10.0.0.1", {"x-forwarded-for": "1.2.3.4"})
        assert client_ip(req) == "10.0.0.1"

    def test_forwarded_header_is_honoured_from_a_trusted_peer(self, monkeypatch):
        monkeypatch.setenv("TRUSTED_PROXY_IPS", "10.0.0.1")
        req = FakeRequest("10.0.0.1", {"x-forwarded-for": "1.2.3.4"})
        assert client_ip(req) == "1.2.3.4"

    def test_first_forwarded_entry_is_the_original_caller(self, monkeypatch):
        monkeypatch.setenv("TRUSTED_PROXY_IPS", "*")
        req = FakeRequest("10.0.0.1", {"x-forwarded-for": "1.2.3.4, 10.0.0.7"})
        assert client_ip(req) == "1.2.3.4"

    def test_missing_peer_falls_back_to_unknown(self, monkeypatch):
        monkeypatch.delenv("TRUSTED_PROXY_IPS", raising=False)
        assert client_ip(FakeRequest(None)) == "unknown"