# -*- coding: utf-8 -*-
"""语料脱敏（阶段 E / 方案 §7.3）测试：纯函数零 IO。

覆盖（每类 1 正 1 负 + 字段名探测 + 自定义规则 + 递归）：
① sk- 前缀（变体前缀 + 长度阈值 {20,} 起步）；
② 平台 token 前缀族合辑（ghp_/gho_/ghu_/ghs_/ghr_/
   github_pat_/glpat-/AIza/xox[abposr]-/hf_/gsk_/xai-/
   npm_/pypi-/dop_v1_/SG./sk_live_/rk_live_/AKIA/ASIA）
   与短前缀负例；
③ PEM 私钥块整块抹除（CERTIFICATE 块不误伤）；
④ URL 内嵌凭证 user:password@host（host 允许域名/
   IPv4/localhost——R6 项目3）；
⑤ 按字段名结构化探测（password/api_key/access_token/
   client_secret/secret/token 类键整值抹除；词边界
   匹配，max_tokens/token_count 不误伤——R6 项目5）；
⑥ 邮箱（默认打码）；
⑦ 手机号；
⑧ 身份证（位数规则，含校验位）；
⑨ 银行卡（13-19 位简化规则）；
⑩ 内网 IPv4（私网/回环/链路本地）；
⑪ IPv6 link-local；
⑫ 绝对路径打码至盘符（含 POSIX /Users//home/ 形态
   ——R6 项目4）；
⑬ 自定义规则（corpus.redact_extra 词/正则，
   parse_extra_rules 词/正则/字典三形态）；
⑭ dict/list 递归（嵌套敏感键 + 字符串列表）；
⑯ URL 凭证 host 放宽（IPv4/localhost，R6 项目3）；
⑰ POSIX 绝对路径正负例（R6 项目4）；
⑱ 敏感键词边界正负例（R6 项目5）。

跑法：py -3.12 -m pytest tests/test_redact.py -v
"""

import pathlib
import sys

BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from proxy_core import redact as R


# ─── ① sk- 前缀 ─────────────────────────────────────

def test_01_sk_token_positive():
    s = "key is sk-abcdefghijklmnopqrstuvwxyz1234 done"
    out, hits = R.redact_text(s)
    assert hits == 1
    assert "sk-abcdefghijklmnopqrstuvwxyz1234" not in out
    assert R.MASK in out


def test_01_sk_token_negative_short():
    # 长度不足 {20,} → 不抹除（阈值负例）
    s = "sk-short"
    out, hits = R.redact_text(s)
    assert hits == 0
    assert out == s


def test_01_sk_variant_prefix_positive():
    # 变体前缀（sk-proj- 等）——变体段计入 {20,} 长度
    s = "使用 sk-proj-abcdefghijklmnopqrstuv 访问"
    out, hits = R.redact_text(s)
    assert hits == 1
    assert "sk-proj-abcdefghijklmnopqrstuv" not in out


# ─── ② 平台 token 前缀族合辑 ─────────────────────────

def test_02_token_family_positive():
    tokens = [
        "gho_" + "a" * 24,
        "ghu_" + "b" * 24,
        "ghs_" + "c" * 24,
        "ghr_" + "d" * 24,
        "github_pat_" + "e" * 24,
        "glpat-" + "f" * 24,
        "AIza" + "g" * 36,
        "xoxb-" + "h" * 20,
        "hf_" + "i" * 24,
        "gsk_" + "j" * 24,
        "xai-" + "k" * 24,
        "npm_" + "l" * 24,
        "pypi-" + "m" * 24,
        "dop_v1_" + "n" * 24,
        "SG." + "o" * 24,
        "sk_live_" + "p" * 24,
        "rk_live_" + "q" * 24,
        "AKIA" + "A" * 16,
        "ASIA" + "B" * 16,
    ]
    s = " ".join(tokens)
    out, hits = R.redact_text(s)
    assert hits == len(tokens)
    for t in tokens:
        assert t not in out
    assert out.count(R.MASK) == len(tokens)


def test_02_token_family_negative_short():
    # 各前缀长度不足 → 全不抹除
    short = [
        "ghp_" + "a" * 10,
        "github_pat_" + "e" * 10,
        "glpat-" + "f" * 10,
        "AIza" + "g" * 10,
        "xoxb-" + "h" * 5,
        "hf_" + "i" * 10,
        "AKIA" + "A" * 10,
        "sk_live_" + "p" * 10,
    ]
    s = " ".join(short)
    out, hits = R.redact_text(s)
    assert hits == 0
    assert out == s


# ─── ③ PEM 私钥块 ───────────────────────────────────

def test_03_pem_private_key_positive():
    pem = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEowIBAAKCAQEA0123456789abcdef\n"
        "-----END RSA PRIVATE KEY-----\n"
    )
    out, hits = R.redact_text("配置如下：\n" + pem + "完毕")
    assert hits == 1
    assert "PRIVATE KEY" not in out
    assert "MIIEowIBAAKCAQEA0123456789abcdef" not in out
    assert out.count(R.MASK) == 1


def test_03_pem_certificate_negative():
    cert = (
        "-----BEGIN CERTIFICATE-----\n"
        "MIIB0123456789abcdef\n"
        "-----END CERTIFICATE-----\n"
    )
    out, hits = R.redact_text(cert)
    assert hits == 0
    assert out == cert


# ─── ④ URL 内嵌凭证 ─────────────────────────────────

def test_04_url_credentials_positive():
    out, hits = R.redact_text(
        "数据库连接 mysql://admin:s3cr3t@db.internal.example.com/prod")
    assert hits == 1
    assert "s3cr3t" not in out
    assert "admin" not in out
    # host 保留
    assert "db.internal.example.com" in out


def test_04_url_plain_negative():
    s = "https://example.com/path?a=1"
    out, hits = R.redact_text(s)
    assert hits == 0
    assert out == s


# ─── ⑤ 按字段名结构化探测 ─────────────────────────────

def test_05_sensitive_key_positive():
    msgs = [
        {"role": "user", "content": "你好",
         "api_key": "sk-abcdefghijklmnopqrstuvwxyz"},
        {"role": "assistant", "content": "好的"},
    ]
    out, hits = R.redact_messages(msgs)
    assert out[0]["api_key"] == R.MASK
    assert out[0]["content"] == "你好"
    assert out[1]["content"] == "好的"
    assert hits == 1


def test_05_sensitive_key_negative():
    msgs = [{"role": "user", "content": "普通文本无敏感键"}]
    out, hits = R.redact_messages(msgs)
    assert hits == 0
    assert out == msgs


def test_05_sensitive_key_variants_positive():
    # password / access_token / client_secret / secret /
    # token 类键（含大小写与复合键名）
    data = {
        "Password": "hunter2",
        "my_access_token": "t" * 30,
        "clientSecret": "c" * 30,
        "auth_token": "a" * 30,
        "user_secret": "u" * 30,
    }
    out, hits = R.redact_messages(data)
    assert hits == 5
    for v in out.values():
        assert v == R.MASK


def test_05_nested_sensitive_key_positive():
    # 递归：嵌套 dict 内敏感键整值抹除，非敏感值保留
    data = {"auth": {"password": "hunter2",
                     "nested": {"client_secret": "s3cr3t",
                                "ok": 1}}}
    out, hits = R.redact_messages(data)
    assert out["auth"]["password"] == R.MASK
    assert out["auth"]["nested"]["client_secret"] == R.MASK
    assert out["auth"]["nested"]["ok"] == 1
    assert hits == 2


# ─── ⑥ 邮箱 ─────────────────────────────────────────

def test_06_email_positive():
    out, hits = R.redact_text("请联系 john.doe@example.com 获取")
    assert hits == 1
    assert "john.doe" not in out
    assert "example.com" not in out


def test_06_email_negative():
    # 无 TLD 点分（user@localhost）→ 不判邮箱
    s = "联系地址 user@localhost 无效"
    out, hits = R.redact_text(s)
    assert hits == 0


# ─── ⑦ 手机号 ───────────────────────────────────────

def test_07_phone_positive():
    out, hits = R.redact_text("请致电 13812345678 联系")
    assert hits == 1
    assert "13812345678" not in out


def test_07_phone_negative():
    # 12 开头（不在 1[3-9] 号段）→ 不抹除
    s = "订单 12345678901 已处理"
    out, hits = R.redact_text(s)
    assert hits == 0
    assert out == s


# ─── ⑧ 身份证 ───────────────────────────────────────

def test_08_id_card_positive():
    out, hits = R.redact_text("身份证号 110101199003077758 已验证")
    assert hits == 1
    assert "110101199003077758" not in out


def test_08_id_card_negative():
    # 12 位（不足 18 位规则）→ 不抹除
    s = "编号 123456789012 短位"
    out, hits = R.redact_text(s)
    assert hits == 0
    assert out == s


# ─── ⑨ 银行卡 ───────────────────────────────────────

def test_09_bank_card_positive():
    out, hits = R.redact_text("银行卡 6222021234567890123 已绑定")
    assert hits == 1
    assert "6222021234567890123" not in out


def test_09_bank_card_negative():
    # 10 位（不足 13 位下限）→ 不抹除
    s = "短号 6222021234 无效"
    out, hits = R.redact_text(s)
    assert hits == 0
    assert out == s


# ─── ⑩ 内网 IPv4 ────────────────────────────────────

def test_10_private_ipv4_positive():
    out, hits = R.redact_text("内网 192.168.1.100 与 10.0.0.8 可达")
    assert hits == 2
    assert "192.168.1.100" not in out
    assert "10.0.0.8" not in out


def test_10_private_ipv4_negative():
    # 公网地址（8.8.8.8）→ 不抹除
    s = "公网 DNS 8.8.8.8 不变"
    out, hits = R.redact_text(s)
    assert hits == 0
    assert out == s


def test_10_loopback_linklocal_v4_positive():
    out, hits = R.redact_text("回环 127.0.0.1 与链路本地 169.254.169.254")
    assert hits == 2
    assert "127.0.0.1" not in out
    assert "169.254.169.254" not in out


# ─── ⑪ IPv6 link-local ──────────────────────────────

def test_11_ipv6_linklocal_positive():
    out, hits = R.redact_text("链路本地 fe80::1 仅本链路")
    assert hits == 1
    assert "fe80::1" not in out


def test_11_ipv6_global_negative():
    # 全球单播地址（非 fe80::/10）→ 不抹除
    s = "全球单播 2001:db8::1 不变"
    out, hits = R.redact_text(s)
    assert hits == 0
    assert out == s


# ─── ⑫ 绝对路径打码至盘符 ─────────────────────────────

def test_12_absolute_path_positive():
    out, hits = R.redact_text("工作区路径 G:\\qidicode\\portable\\main.py 请检查")
    assert hits == 1
    assert out == "工作区路径 G:\\*** 请检查"


def test_12_relative_path_negative():
    # 相对路径（无盘符）→ 不抹除
    s = "相对路径 qidicode/portable/main.py 不含盘符"
    out, hits = R.redact_text(s)
    assert hits == 0
    assert out == s


# ─── ⑬ 自定义规则（corpus.redact_extra）──────────────

def test_13_extra_word_and_regex_positive():
    extra = R.parse_extra_rules(["内部代号", r"v\d+\.\d+"])
    out, hits = R.redact_text("内部代号 alpha，版本 v1.2 发布", extra)
    assert hits == 2
    assert "内部代号" not in out
    assert "v1.2" not in out


def test_13_extra_negative_without_rules():
    # 未传 extra → 自定义词不生效（纯函数零 IO：不读 config）
    s = "内部代号 alpha，版本 v1.2 发布"
    out, hits = R.redact_text(s)
    assert hits == 0
    assert out == s


def test_13_extra_dict_regex_positive():
    extra = R.parse_extra_rules([{"regex": "case-\\d{4}"}])
    out, hits = R.redact_text("单号 case-1234 已归档", extra)
    assert hits == 1
    assert "case-1234" not in out


def test_13_extra_bad_entries_skipped():
    # 坏型条目（None/空串/坏正则）跳过，不抛错
    extra = R.parse_extra_rules([None, "", "ok", "([bad"])
    assert len(extra) == 1  # 仅 "ok" 词规则
    out, hits = R.redact_text("ok 良", extra)
    assert hits == 1


# ─── ⑭ dict/list 递归 ───────────────────────────────

def test_14_recursive_list_of_strings_positive():
    out, hits = R.redact_messages(
        ["sk-abcdefghijklmnopqrstuvwxyz", "plain"])
    assert out[0] == R.MASK
    assert out[1] == "plain"
    assert hits == 1


def test_14_recursive_multimodal_blocks_positive():
    # 多模态 content 块列表：文本块内邮箱打码，结构保留
    msgs = [{
        "role": "user",
        "content": [
            {"type": "text", "text": "邮箱 a@b.com"},
            {"type": "image_url",
             "image_url": {"url": "https://x/y.png"}},
        ],
    }]
    out, hits = R.redact_messages(msgs)
    assert out[0]["content"][0]["text"] == "邮箱 " + R.MASK
    assert out[0]["content"][1]["type"] == "image_url"
    assert hits == 1


def test_14_non_string_values_preserved():
    data = {"n": 42, "f": 1.5, "b": True, "nil": None,
            "s": "sk-abcdefghijklmnopqrstuvwxyz"}
    out, hits = R.redact_messages(data)
    assert out["n"] == 42
    assert out["f"] == 1.5
    assert out["b"] is True
    assert out["nil"] is None
    assert out["s"] == R.MASK
    assert hits == 1


def test_15_pure_no_io_same_result():
    # 纯函数：同输入同输出（无状态、无 IO）
    s = "token sk-abcdefghijklmnopqrstuvwxyz 与 13812345678"
    out1, hits1 = R.redact_text(s)
    out2, hits2 = R.redact_text(s)
    assert out1 == out2
    assert hits1 == hits2 == 2


# ─── ⑯ URL 凭证 host 形态放宽（R6 项目3）──────────


def test_16_url_credentials_ip_host_positive():
    # host 为内网 IPv4：凭证段同样打掉
    # （IP 本体由内网私网规则另行抹除）
    out, hits = R.redact_text(
        "mysql://root:123456@10.0.0.5:3306/db")
    assert "root" not in out
    assert "123456" not in out
    assert "10.0.0.5" not in out
    assert hits == 2  # URL 凭证 1 + 内网 IPv4 1


def test_16_url_credentials_localhost_host_positive():
    # host 为 localhost：凭证段打掉，localhost 保留
    out, hits = R.redact_text(
        "postgres://admin:s3cret@localhost:5432/app")
    assert "s3cret" not in out
    assert "admin" not in out
    assert "localhost:5432" in out
    assert hits == 1


# ─── ⑰ POSIX 绝对路径（R6 项目4）─────────────────


def test_17_posix_absolute_path_positive():
    out, hits = R.redact_text(
        "工作区路径 /Users/admin/secret.txt 请查")
    assert out == "工作区路径 /Users/*** 请查"
    assert hits == 1
    out, hits = R.redact_text(
        "数据文件 /home/u/data.csv 归档")
    assert out == "数据文件 /home/*** 归档"
    assert hits == 1


def test_17_posix_path_negative():
    # 非 /Users//home/ 形态的 POSIX 路径不误伤
    s = "/etc/passwd 与 ./rel/x 与 qidicode/main.py"
    out, hits = R.redact_text(s)
    assert hits == 0
    assert out == s


# ─── ⑱ 敏感键词边界（R6 项目5）───────────────────


def test_18_sensitive_key_boundary_negative():
    # 含 token/key 子串但非敏感键（旧子串匹配会误伤）
    data = {"max_tokens": 4096, "token_count": 3,
            "key_count": 2, "tokens_per_minute": 60,
            "monkey": "m", "keyring": "k"}
    out, hits = R.redact_messages(data)
    assert hits == 0
    assert out == data


def test_18_sensitive_key_boundary_positive():
    # 词边界命中：词尾 token/key（auth_token/openai_api_key）
    # + 任意位置 password/credential（password_hash/credential_store）
    # + 驼峰复合 clientSecret
    data = {"auth_token": "a" * 8,
            "openai_api_key": "o" * 8,
            "password_hash": "p" * 8,
            "credential_store": "c" * 8,
            "clientSecret": "s" * 8}
    out, hits = R.redact_messages(data)
    assert hits == 5
    for v in out.values():
        assert v == R.MASK
