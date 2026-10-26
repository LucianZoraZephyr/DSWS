"""S5 · 策略契约加载器的测试。

契约文件本身也要被校验：**少一个键、多一个键、值不合理，都必须报错**。
策略参数如果可以被「写错但不报错」，那它就不是契约，只是一段建议。
"""

from __future__ import annotations

import copy

import pytest
import yaml

from app.security.config import (
    POLICY_PATH,
    PolicyConfigError,
    load_policy_config,
    parse_policy_config,
)
from app.security.corpus import CorpusError, load_corpus


@pytest.fixture()
def raw() -> dict:
    data = yaml.safe_load(POLICY_PATH.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return copy.deepcopy(data)


# ---------------------------------------------------------------------------
# 契约本身
# ---------------------------------------------------------------------------


def test_contract_loads() -> None:
    config = load_policy_config()
    assert config.version == 1
    assert config.default_limit <= config.max_limit
    assert config.account_grants == ("SELECT",)


def test_contract_file_is_the_one_we_think_it_is() -> None:
    assert POLICY_PATH.name == "sql.policy.yaml"
    assert POLICY_PATH.parent.name == "contracts"


# ---------------------------------------------------------------------------
# 严格性
# ---------------------------------------------------------------------------


def test_missing_key_raises(raw: dict) -> None:
    del raw["limits"]
    with pytest.raises(PolicyConfigError, match="limits"):
        parse_policy_config(raw)


def test_unknown_key_raises(raw: dict) -> None:
    raw["statement"]["allow_whatever"] = True
    with pytest.raises(PolicyConfigError, match="未定义"):
        parse_policy_config(raw)


def test_unknown_section_raises(raw: dict) -> None:
    raw["extra_section"] = {}
    with pytest.raises(PolicyConfigError, match="未定义"):
        parse_policy_config(raw)


def test_bad_version_raises(raw: dict) -> None:
    raw["version"] = 2
    with pytest.raises(PolicyConfigError, match="version"):
        parse_policy_config(raw)


def test_limit_inversion_raises(raw: dict) -> None:
    raw["limits"]["default_limit"] = raw["limits"]["max_limit"] + 1
    with pytest.raises(PolicyConfigError, match="default_limit"):
        parse_policy_config(raw)


def test_non_positive_limit_raises(raw: dict) -> None:
    raw["limits"]["max_limit"] = 0
    with pytest.raises(PolicyConfigError, match="max_limit"):
        parse_policy_config(raw)


def test_non_select_grant_raises(raw: dict) -> None:
    raw["account"]["grants"] = ["SELECT", "INSERT"]
    with pytest.raises(PolicyConfigError, match="SELECT"):
        parse_policy_config(raw)


def test_channel_code_must_be_in_codes(raw: dict) -> None:
    raw["errors"]["channel_code"] = "SOMETHING_ELSE"
    with pytest.raises(PolicyConfigError, match="channel_code"):
        parse_policy_config(raw)


def test_duplicate_keywords_raise(raw: dict) -> None:
    raw["statement"]["forbid_write_keywords"] = ["DROP", "DROP"]
    with pytest.raises(PolicyConfigError, match="重复"):
        parse_policy_config(raw)


def test_keyword_in_both_lists_raises(raw: dict) -> None:
    raw["statement"]["forbid_dangerous_keywords"] = ["SLEEP", "DROP"]
    with pytest.raises(PolicyConfigError, match="DROP"):
        parse_policy_config(raw)


# ---------------------------------------------------------------------------
# 语料夹具的严格性
# ---------------------------------------------------------------------------


def test_corpus_loads() -> None:
    corpus = load_corpus()
    assert corpus.reject
    assert corpus.allow
    assert len(corpus) == len(corpus.reject) + len(corpus.allow)


def test_corpus_rejects_a_duplicate_id(tmp_path) -> None:
    source = load_corpus()
    path = tmp_path / "corpus.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "reject": [
                    {"id": "dup", "sql": "SELECT 1", "reason_code": "SQL_EMPTY"},
                    {"id": "dup", "sql": "SELECT 2", "reason_code": "SQL_EMPTY"},
                ],
                "allow": [{"id": "ok", "sql": "SELECT 1"}],
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    with pytest.raises(CorpusError, match="重复"):
        load_corpus(path)
    assert source.reject


def test_corpus_rejects_an_unknown_reason_code(tmp_path) -> None:
    path = tmp_path / "corpus.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "reject": [
                    {"id": "x", "sql": "SELECT 1", "reason_code": "NOT_A_REAL_CODE"},
                ],
                "allow": [{"id": "ok", "sql": "SELECT 1"}],
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    with pytest.raises(CorpusError, match="不在策略契约里"):
        load_corpus(path)


def test_corpus_rejects_a_missing_section(tmp_path) -> None:
    path = tmp_path / "corpus.yaml"
    path.write_text(
        yaml.safe_dump({"version": 1, "reject": [{"id": "x", "sql": "SELECT 1"}]}),
        encoding="utf-8",
    )
    with pytest.raises(CorpusError, match="allow"):
        load_corpus(path)
