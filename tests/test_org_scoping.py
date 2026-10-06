"""Per-organisation bot and index resolution (Section 3.4).

Assignment on disk is not access control unless the index follows it. These
tests pin the part that is easy to get wrong: two organisations both holding a
"finance" workspace must not share one vector store, and a citation minted for
one must not open a document belonging to the other.
"""

import os

import pytest
from fastapi.testclient import TestClient

from chatbot import content_library
from chatbot.admin_store import AdminStore
from chatbot.api import create_app
from chatbot.content_library import index_dir_for, list_documents
from chatbot.documents import DocumentNotFound, resolve_document
from chatbot.session_store import SessionStore
from chatbot.users import SAMPLE_ORGANISATION_ID, UserStore
from chatbot.workspaces import organisation_workspace_config


class OrgBot:
    """Stand-in for RAGBot that records which data dir it was built with."""

    built = []

    def __init__(self, category, data_dir=None, store_path=None):
        self.category = category
        self.data_dir = data_dir
        self.store_path = store_path
        self.ingest_calls = 0
        self.ingested_dir = None
        self.ask_calls = []
        OrgBot.built.append((data_dir, store_path))

    class _Store:
        def __init__(self):
            self.n = 0

        def count(self):
            return self.n

    def read_and_embed_data(self, folder_path=None):
        self.ingest_calls += 1
        self.ingested_dir = folder_path or self.data_dir
        return type(
            "S",
            (),
            {
                "documents_seen": 1,
                "documents_reindexed": 1,
                "chunks_upserted": 2,
                "stale_chunks_removed": 0,
                "documents_failed": 0,
            },
        )()

    def ask(self, question=None, k=3, source=None, history=None, **kw):
        self.ask_calls.append(question)
        self.last_results = []
        return f"reply in {self.category} for {self.data_dir}"


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Config-based app (no injected registry), so org scoping actually engages."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("INDEX_DIR", str(tmp_path / "index"))
    monkeypatch.setenv("WORKSPACES", "finance,hr")
    monkeypatch.setattr("chatbot.api.make_workspace_bot",
                        lambda category, config=None: OrgBot(category, **(config or {})))
    OrgBot.built = []
    app = create_app(
        session_store=SessionStore(db_path=str(tmp_path / "s.sqlite3")),
        user_store=UserStore(db_path=str(tmp_path / "u.sqlite3"), secret="test-secret"),
        admin_store=AdminStore(str(tmp_path / "a.sqlite3")),
        api_key="k",
        rate_limit=0,
    )
    return TestClient(app), tmp_path


def auth():
    return {"authorization": "Bearer k"}


def make_org(c, org_id):
    res = c.post("/admin/organisations", headers=auth(), json={"id": org_id, "name": org_id})
    assert res.status_code == 200, res.text
    return res.json()


def assign(c, org_id, filename, categories=("finance",)):
    root = content_library.library_root()
    root.mkdir(parents=True, exist_ok=True)
    (root / filename).write_bytes(f"%PDF {org_id}".encode())
    res = c.post("/admin/library/assign", headers=auth(),
                 json={"organisation_id": org_id, "categories": list(categories),
                       "filenames": [filename]})
    assert res.status_code == 200, res.text
    return res.json()


def member_token(c, email, org_id, category="finance"):
    """Create a user inside `org_id` and return its session token.

    Goes through /auth/login rather than minting a token directly, so the token
    carries the same organisation claim a real session would.
    """
    users = c.app.state.user_store
    users.create_user(email, "password123", email.split("@")[0], category,
                      organisation_id=org_id)
    res = c.post("/auth/login", json={"email": email, "password": "password123"})
    assert res.status_code == 200, res.text
    return res.json()["token"]


class TestOrganisationIndexIsolation:
    def test_index_path_is_per_organisation(self):
        assert index_dir_for("acme", "finance") != index_dir_for("globex", "finance")
        assert index_dir_for("acme", "finance") != index_dir_for("acme", "hr")

    def test_index_path_is_under_the_index_root(self, tmp_path, monkeypatch):
        monkeypatch.setenv("INDEX_DIR", str(tmp_path / "idx"))
        assert str(index_dir_for("acme", "finance")).startswith(str(tmp_path / "idx"))

    def test_config_points_data_dir_at_the_served_copy(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
        conf = organisation_workspace_config("acme", "finance")
        assert conf["data_dir"] == str(content_library.workspace_dir("acme", "finance"))
        assert "organisations" in conf["data_dir"]

    def test_config_rejects_a_traversal_organisation(self):
        with pytest.raises(ValueError):
            organisation_workspace_config("../escape", "finance")

    def test_two_organisations_get_two_different_indexes(self, env):
        c, _ = env
        make_org(c, "acme")
        make_org(c, "globex")
        assign(c, "acme", "acme.pdf")
        assign(c, "globex", "globex.pdf")

        stores = {store for _data, store in OrgBot.built}
        assert len(stores) == 2, "both organisations resolved to one index"

    def test_bot_data_dir_matches_the_organisation_served_copy(self, env):
        c, _ = env
        make_org(c, "acme")
        assign(c, "acme", "acme.pdf")
        (data_dir, _store) = next(
            pair for pair in OrgBot.built
            if pair[0] and str(content_library.workspace_dir("acme", "finance")) in pair[0]
        )
        assert list_documents("acme", "finance") == ["acme.pdf"]

    def test_bots_are_cached_per_organisation_and_workspace(self, env):
        """A cached handle keeps its vector store open between requests."""
        c, _ = env
        make_org(c, "acme")
        assign(c, "acme", "acme.pdf")
        before = len(OrgBot.built)
        token = member_token(c, "a@acme.com", "acme")
        c.post("/chat", headers={"authorization": f"Bearer {token}"},
               json={"question": "hello", "session_id": None})
        assert len(OrgBot.built) == before, "a second bot was constructed for the same scope"


class TestAssignmentReindexesTheRightIndex:
    def test_assignment_indexes_the_organisation_workspace(self, env):
        c, _ = env
        make_org(c, "acme")
        assign(c, "acme", "acme.pdf")
        key = (str(content_library.workspace_dir("acme", "finance")),
               str(index_dir_for("acme", "finance")))
        assert key in OrgBot.built

    def test_assignment_does_not_touch_the_global_workspace_index(self, env):
        c, _ = env
        make_org(c, "acme")
        assign(c, "acme", "acme.pdf")
        assert all(
            data and content_library.ORGANISATIONS_DIR_NAME in data
            for data, _store in OrgBot.built
        ), "a category-global workspace bot was built"

    def test_unassignment_reindexes_too(self, env):
        """Removing content must reindex, or the index keeps serving a deleted file."""
        c, _ = env
        make_org(c, "acme")
        assign(c, "acme", "acme.pdf")
        store = str(index_dir_for("acme", "finance"))
        bot = c.app.state.org_bots[("acme", "finance")]
        assert bot.store_path == store
        before = bot.ingest_calls
        c.post("/admin/library/unassign", headers=auth(),
               json={"organisation_id": "acme", "categories": ["finance"],
                     "filenames": ["acme.pdf"]})
        assert bot.ingest_calls == before + 1
        assert list_documents("acme", "finance") == []

    def test_assignment_of_several_workspaces_indexes_each(self, env):
        c, _ = env
        make_org(c, "acme")
        assign(c, "acme", "a.pdf", categories=("finance", "hr"))
        for cat in ("finance", "hr"):
            assert str(index_dir_for("acme", cat)) in {s for _d, s in OrgBot.built}


class TestOrganisationCitationResolution:
    def test_resolves_within_the_organisation(self, env):
        c, _ = env
        make_org(c, "acme")
        assign(c, "acme", "acme.pdf")
        path = resolve_document("finance", "acme.pdf", "acme")
        assert path == content_library.workspace_dir("acme", "finance") / "acme.pdf"

    def test_another_organisations_document_is_not_resolvable(self, env):
        c, _ = env
        make_org(c, "acme")
        make_org(c, "globex")
        assign(c, "globex", "globex-secret.pdf")
        with pytest.raises(DocumentNotFound):
            resolve_document("finance", "globex-secret.pdf", "acme")

    def test_traversal_out_of_the_served_copy_is_refused(self, env):
        c, _ = env
        make_org(c, "acme")
        make_org(c, "globex")
        assign(c, "globex", "secret.pdf")
        with pytest.raises(DocumentNotFound):
            resolve_document("finance", "../../globex/finance/secret.pdf", "acme")

    def test_other_workspace_in_the_same_org_is_not_reachable(self, env):
        c, _ = env
        make_org(c, "acme")
        assign(c, "acme", "hr-only.pdf", categories=("hr",))
        with pytest.raises(DocumentNotFound):
            resolve_document("finance", "hr-only.pdf", "acme")

    def test_legacy_material_stays_reachable(self, env):
        """Content uploaded before organisations existed must not be stranded."""
        c, tmp_path = env
        legacy = tmp_path / "data" / "finance"
        legacy.mkdir(parents=True, exist_ok=True)
        (legacy / "legacy.pdf").write_bytes(b"%PDF legacy")
        assert resolve_document("finance", "legacy.pdf", "acme").read_bytes() == b"%PDF legacy"

    def test_served_copy_wins_over_a_legacy_file_of_the_same_name(self, env):
        c, tmp_path = env
        make_org(c, "acme")
        assign(c, "acme", "dup.pdf")
        legacy = tmp_path / "data" / "finance"
        legacy.mkdir(parents=True, exist_ok=True)
        (legacy / "dup.pdf").write_bytes(b"%PDF legacy version")
        assert resolve_document("finance", "dup.pdf", "acme").read_bytes() == b"%PDF acme"

    def test_unknown_organisation_is_not_resolvable(self, env):
        c, _ = env
        make_org(c, "acme")
        assign(c, "acme", "acme.pdf")
        with pytest.raises(DocumentNotFound):
            resolve_document("finance", "acme.pdf", "ghost")


class TestOrganisationDownloadEndpoint:
    def test_download_uses_the_organisation_served_copy(self, env):
        c, _ = env
        make_org(c, "acme")
        assign(c, "acme", "acme.pdf")
        token = member_token(c, "a@acme.com", "acme")
        res = c.get("/documents/download", headers={"authorization": f"Bearer {token}"},
                    params={"source": "acme.pdf"})
        assert res.status_code == 200, res.text
        assert res.content == b"%PDF acme"

    def test_download_of_another_organisations_document_is_404(self, env):
        c, _ = env
        make_org(c, "acme")
        make_org(c, "globex")
        assign(c, "globex", "globex.pdf")
        token = member_token(c, "a@acme.com", "acme")
        res = c.get("/documents/download", headers={"authorization": f"Bearer {token}"},
                    params={"source": "globex.pdf"})
        assert res.status_code == 404

    def test_moving_a_user_between_organisations_revokes_their_link(self, env):
        """The download token is signed, but the org is re-read from the user row."""
        c, _ = env
        make_org(c, "acme")
        make_org(c, "globex")
        assign(c, "acme", "acme.pdf")
        token = member_token(c, "a@acme.com", "acme")
        users = c.app.state.user_store
        user = next(
            u for u in users.list_users() if u["email"] == "a@acme.com"
        )
        users.set_user_organisation(user["id"], "globex")
        res = c.get("/documents/download", headers={"authorization": f"Bearer {token}"},
                    params={"source": "acme.pdf"})
        assert res.status_code == 404

    def test_sample_organisation_serves_the_launch_pack(self, env):
        c, tmp_path = env
        root = content_library.library_root()
        root.mkdir(parents=True, exist_ok=True)
        (root / "overview_of_esg.pdf").write_bytes(b"%PDF orientation")
        assert content_library.seed_sample_pack(categories=["finance"])
        res = c.post("/auth/signup", json={
            "email": "free@visitor.com", "password": "password123",
            "name": "V", "category": "finance",
        })
        assert res.status_code == 200, res.text
        token = res.json()["token"]
        res = c.get("/documents/download", headers={"authorization": f"Bearer {token}"},
                    params={"source": "overview_of_esg.pdf"})
        assert res.status_code == 200, res.text


class TestExplicitRegistryKeepsPrecedence:
    def test_injected_registry_is_not_overridden_by_org_scoping(self, tmp_path, monkeypatch):
        """A caller that supplies its own bots means them for every organisation."""
        monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
        injected = OrgBot("finance", data_dir="injected", store_path="injected")
        app = create_app(
            session_store=SessionStore(db_path=str(tmp_path / "s.sqlite3")),
            user_store=UserStore(db_path=str(tmp_path / "u.sqlite3"), secret="s"),
            workspaces={"finance": injected},
            api_key="k",
            rate_limit=0,
        )
        c = TestClient(app)
        c.post("/admin/organisations", headers=auth(), json={"id": "acme", "name": "Acme"})
        token = member_token(c, "a@acme.com", "acme")
        # Not a greeting: smalltalk answers those without reaching the bot, so
        # this asserts the bot was actually the one asked.
        c.post("/chat", headers={"authorization": f"Bearer {token}"},
               json={"question": "Summarise our IFRS S2 exposure.", "session_id": None})
        assert injected.ask_calls == ["Summarise our IFRS S2 exposure."]