#!/usr/bin/env python3
"""LEGACY-COMPAT: black-box differential tests of mihomo-build-config (blind).

Spec: docs/dev/2026-11-14-spec-v3-legacy-compat.md, FR-LC-01..04.
Run:  python3 tests/legacy-compat-differential.py [--real-rules-dir DIR]

References are taken from git history and executed as black boxes only:
  6b8a52d  legacy reference (main)        - for inputs WITHOUT cascade-contract
  e6db054  r7 V3 reference                - for inputs WITH cascade-contract
The builder under test is usr/local/sbin/mihomo-build-config of the working tree.

Each case: identical fresh sandboxes (MIHOMO_BASE), file:// subscription, rules in
routing-rules.yaml (no routing-rules.url). Compared: exit code and the bytes of every
file under MIHOMO_BASE (inputs included: an unchanged input must stay unchanged, and
any created/modified output must match). stdout/stderr text is NOT compared.
Normalization: (1) the per-sandbox temp path is replaced by <SANDBOX> in file contents
(subscription.url contains it; harness artifact). (2) Otherwise none by default. If both runs of the *same* reference builder differ
from each other (inherent nondeterminism), the differing lines are located by the
self-check below and masked as `<VOLATILE>` for that file; a case reports this.
Needs no network, no root, no PyYAML. V3 mode = file `cascade-contract` in MIHOMO_BASE.
"""
import argparse
import base64
import os
import re
import runpy
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
BUILDER = ROOT / 'usr/local/sbin/mihomo-build-config'
LEGACY_REV, V3_REV = '6b8a52d', 'e6db054'
REL = 'usr/local/sbin/mihomo-build-config'

TMP = None
FAILS = []
RESULTS = []


def git_show(rev):
    out = subprocess.run(['git', '-C', str(ROOT), 'show', f'{rev}:{REL}'],
                         capture_output=True, check=True).stdout
    p = Path(TMP) / f'ref-{rev}.py'
    p.write_bytes(out)
    return p


def vless(host, name):
    return ('vless://11111111-2222-3333-4444-555555555555@'
            f'{host}.example.net:443?security=tls&type=grpc&sni={host}.example.net'
            f'&serviceName=ABC&encryption=none#' + quote(name))


def sub_text(names):
    return base64.b64encode(
        '\n'.join(vless(f'h{i}', n) for i, n in enumerate(names)).encode() + b'\n')


def names_for(aliases, prefix=True):
    flags = {'de': '\U0001F1E9\U0001F1EA', 'ee': '\U0001F1EA\U0001F1EA',
             'pl': '\U0001F1F5\U0001F1F1'}
    out = []
    for a in aliases:
        fl = flags.get(a[:2], '') + ' ' if prefix else ''
        out.append(f'{fl}{a.upper()} {a} node' if a[:2] in flags else f'{a.upper()} {a} node')
    return out


def make_sandbox(root, sub, rules, contract=None, extra=None):
    base = Path(root) / 'base'
    base.mkdir(parents=True)
    for n in ('config.base.yaml', 'iso3166_alpha2.txt'):
        shutil.copyfile(ROOT / 'etc/mihomo' / n, base / n)
    (base / 'local-rules.yaml').write_text('rules: []\n')
    sp = Path(root) / 'sub.txt'
    sp.write_bytes(sub)
    (base / 'subscription.url').write_text(f'file://{sp}\n')
    (base / 'routing-rules.yaml').write_text(rules)
    if contract is not None:
        (base / 'cascade-contract').write_text(contract)
    for k, v in (extra or {}).items():
        (base / k).write_text(v)
    return base, sp


def run(builder, base):
    env = dict(os.environ, MIHOMO_BASE=str(base), MIHOMO_MIN_NODES_ABS='1',
               MIHOMO_MIN_NODES_RATIO='0.1')
    env.pop('MIHOMO_ALLOW_SHRINK', None)
    r = subprocess.run([sys.executable, str(builder)], env=env, cwd=str(base.parent),
                       capture_output=True, text=True, timeout=60)
    return r.returncode


def snap(base):
    # The sandbox's own path (it differs per sandbox by construction) is replaced by
    # <SANDBOX> in file contents: harness artifact, not builder behavior.
    root = str(base.parent).encode()
    return {p.relative_to(base).as_posix(): p.read_bytes().replace(root, b'<SANDBOX>')
            for p in sorted(base.rglob('*')) if p.is_file()}


def scenario(ref, sub, rules, contract=None, seed=None, extra=None, builders=None):
    """Run every builder in `builders` in its own fresh sandbox. `seed` = (sub, rules):
    first a reference-builder run with that input creates a previous config/state, then
    the case input replaces it. Returns [(rc, snapshot)] per builder."""
    res = []
    for b in builders:
        d = tempfile.mkdtemp(prefix='lc-', dir=TMP)
        base, sp = make_sandbox(d, seed[0] if seed else sub,
                                seed[1] if seed else rules, contract, extra)
        if seed:
            run(ref, base)
            sp.write_bytes(sub)
            (base / 'routing-rules.yaml').write_text(rules)
        rc = run(b, base)
        res.append((rc, snap(base)))
    return res


def diff_reason(a, b):
    if a[0] != b[0]:
        return f'exit code ref={a[0]} new={b[0]}'
    ka, kb = set(a[1]), set(b[1])
    if ka != kb:
        return f'file set differs: only ref {sorted(ka - kb)}, only new {sorted(kb - ka)}'
    bad = [k for k in sorted(ka) if a[1][k] != b[1][k]]
    return f'content differs in {", ".join(bad)}' if bad else None


def volatile_mask(ref, sub, rules, contract, seed, extra):
    """Self-check: run the reference twice; files that differ between two runs of the
    SAME builder are inherently nondeterministic (e.g. timestamps). Their differing
    lines are masked before comparing with the builder under test."""
    r1, r2 = scenario(ref, sub, rules, contract, seed, extra, [ref, ref])
    mask = {}
    for k in r1[1]:
        if k in r2[1] and r1[1][k] != r2[1][k]:
            l1, l2 = r1[1][k].splitlines(), r2[1][k].splitlines()
            mask[k] = {i for i, (x, y) in enumerate(zip(l1, l2)) if x != y} \
                if len(l1) == len(l2) else 'all'
    return mask


def apply_mask(res, mask):
    rc, files = res
    out = {}
    for k, v in files.items():
        if k in mask:
            if mask[k] == 'all':
                v = b'<VOLATILE>'
            else:
                v = b'\n'.join(b'<VOLATILE>' if i in mask[k] else line
                               for i, line in enumerate(v.splitlines()))
        out[k] = v
    return rc, out


def case(name, ref, sub, rules, contract=None, seed=None, extra=None, expect_rc=None):
    try:
        mask = volatile_mask(ref, sub, rules, contract, seed, extra)
        r_ref, r_new = scenario(ref, sub, rules, contract, seed, extra, [ref, BUILDER])
        r_ref, r_new = apply_mask(r_ref, mask), apply_mask(r_new, mask)
        why = diff_reason(r_ref, r_new)
        note = f' [rc={r_ref[0]}' + (f', volatile={sorted(mask)}' if mask else '') + ']'
        if why is None and expect_rc is not None and r_ref[0] != expect_rc:
            why = f'harness: reference rc={r_ref[0]}, case meant to give {expect_rc}'
    except Exception as e:  # harness failure is a FAIL too
        why, note = f'harness error: {e!r}', ''
    status = 'FAIL' if why else 'PASS'
    if why:
        FAILS.append(name)
    print(f'{status} {name}{note}' + (f' - {why}' if why else ''), flush=True)


# ---------------------------------------------------------------- fixtures
def rules_text(rule_lines, extra_head=''):
    return extra_head + 'rules:\n' + ''.join(f'  - "{r}"\n' for r in rule_lines)


ALL = ['de', 'de2', 'de3', 'ee', 'pl']
TYPICAL_RULES = rules_text(['DOMAIN-SUFFIX,a.example,de', 'DOMAIN-SUFFIX,b.example,de2',
                            'DOMAIN-SUFFIX,c.example,de3', 'DOMAIN-SUFFIX,d.example,ee',
                            'DOMAIN-SUFFIX,e.example,pl', 'MATCH,DIRECT'])


def legacy_cases(ref, prefix=''):
    sub_all = sub_text(names_for(ALL))
    case(prefix + 'FR-LC-01 typical aliases with prefixes', ref, sub_all, TYPICAL_RULES)
    case(prefix + 'FR-LC-01 typical aliases, no flag prefix', ref,
         sub_text(names_for(ALL, prefix=False)), TYPICAL_RULES)
    v2 = rules_text(['DOMAIN-SUFFIX,a.example,ee-failover', 'DOMAIN-SUFFIX,b.example,de',
                     'DOMAIN-SUFFIX,probe.example,pl', 'MATCH,DIRECT'],
                    'cascade-fallbacks: {"ee":["pl","de"]}\ncascade-probes: ["pl"]\n')
    case(prefix + 'FR-LC-01 v2 fallbacks+probes', ref, sub_all, v2)
    case(prefix + 'FR-LC-01 v2 fallbacks, primary absent from subscription', ref,
         sub_text(names_for(['de', 'pl'])), v2)
    case(prefix + 'FR-LC-01 policy that is not an alias (Telegram)', ref, sub_all,
         rules_text(['DOMAIN-SUFFIX,t.me,Telegram', 'DOMAIN-SUFFIX,a.example,de',
                     'MATCH,DIRECT']))
    case(prefix + 'FR-LC-01 non-alias policy only', ref, sub_all,
         rules_text(['DOMAIN-SUFFIX,t.me,Telegram', 'MATCH,DIRECT']))
    amb = sub_text(['DE de3 node', 'DE de3 backup node', 'EE ee node'])
    case(prefix + 'FR-LC-01 ambiguous alias used in rules', ref, amb,
         rules_text(['DOMAIN-SUFFIX,a.example,de3', 'DOMAIN-SUFFIX,b.example,ee',
                     'MATCH,DIRECT']))
    case(prefix + 'FR-LC-01 ambiguous alias unused in rules', ref, amb,
         rules_text(['DOMAIN-SUFFIX,b.example,ee', 'MATCH,DIRECT']))
    no_de = sub_text(names_for(['ee', 'pl']))
    use_de = rules_text(['DOMAIN-SUFFIX,a.example,de', 'DOMAIN-SUFFIX,b.example,ee',
                         'MATCH,DIRECT'])
    case(prefix + 'FR-LC-01 policy without node, previous config present', ref, no_de,
         use_de, seed=(sub_all, use_de))
    case(prefix + 'FR-LC-01 policy without node, no previous config', ref, no_de, use_de)
    case(prefix + 'FR-LC-01 first build, no previous state', ref, sub_all, use_de)
    case(prefix + 'FR-LC-01 rebuild over previous state (same input)', ref, sub_all,
         use_de, seed=(sub_all, use_de))
    case(prefix + 'FR-LC-01 ambiguous alias with previous config', ref, amb,
         rules_text(['DOMAIN-SUFFIX,a.example,de3', 'MATCH,DIRECT']),
         seed=(sub_all, rules_text(['DOMAIN-SUFFIX,a.example,de3', 'MATCH,DIRECT'])))


V3_HEAD = ('cascade-contract: 3\ncascade-generation: 36\n'
           'cascade-nodes: {"n35-pl": {"server": "pl.example"}, '
           '"n38-de3": {"server": "de3.example"}}\n'
           'cascade-services: {"svc-de3": ["n38-de3"], "svc-pl": ["n35-pl", "n38-de3"]}\n'
           'cascade-probes: ["n38-de3"]\n')


def v3_cases(ref, prefix=''):
    sub = sub_text(['PL pl node', 'DE de3 node', 'EE ee node'])
    good = rules_text(['DOMAIN-SUFFIX,php.net,svc-de3', 'DOMAIN-SUFFIX,x.example,svc-pl',
                       'MATCH,DIRECT'], V3_HEAD)
    case(prefix + 'FR-LC-02 v3 valid document', ref, sub, good, contract='3\n')
    case(prefix + 'FR-LC-02 v3 valid, previous config', ref, sub, good, contract='3\n',
         seed=(sub, good))
    case(prefix + 'FR-LC-02 v3 unknown policy/alias refusal', ref, sub,
         rules_text(['DOMAIN-SUFFIX,php.net,svc-unknown', 'MATCH,DIRECT'], V3_HEAD),
         contract='3\n')
    case(prefix + 'FR-LC-02 v3 non-alias policy (Telegram) refusal', ref, sub,
         rules_text(['DOMAIN-SUFFIX,t.me,Telegram', 'MATCH,DIRECT'], V3_HEAD),
         contract='3\n')
    case(prefix + 'FR-LC-02 v3 node alias absent from subscription', ref,
         sub_text(['PL pl node', 'EE ee node']), good, contract='3\n')
    case(prefix + 'FR-LC-02 v3 ambiguous node alias', ref,
         sub_text(['PL pl node', 'DE de3 node', 'DE de3 backup node']), good,
         contract='3\n')
    case(prefix + 'FR-LC-02 v3 document without cascade-contract file (opt-in refusal)',
         ref, sub, good)
    case(prefix + 'FR-LC-02 contract file present, legacy document', ref,
         sub_text(names_for(ALL)), TYPICAL_RULES, contract='3\n')


# ------------------------------------------------------------ FR-LC-03 real
def real_aliases(text, v3):
    pols, aliases = set(), set()
    for m in re.finditer(r'^\s*-\s*[\'"]?([^\n\'"#]+)', text, re.M):
        parts = m.group(1).strip().split(',')
        if len(parts) >= 3 and re.match(r'^[A-Z-]+$', parts[0]):
            pols.add(parts[2].strip() if parts[0] != 'MATCH' else parts[1].strip())
        elif parts[0] == 'MATCH' and len(parts) >= 2:
            pols.add(parts[1].strip())
    for p in pols:
        if p in ('DIRECT', 'REJECT', 'REJECT-DROP', 'PASS', 'COMPATIBLE'):
            continue
        aliases.add(re.sub(r'-failover$', '', p))
    for m in re.finditer(r'"([a-z]{2}\d*|n\d+-[a-z0-9]+)"', text):
        aliases.add(m.group(1))
    out = set()
    for a in aliases:
        a = re.sub(r'^svc-', '', a)
        a = a.split('-', 1)[1] if v3 and re.match(r'^n\d+-', a) else a
        out.add(a)
    return sorted(out)


def real_cases(ref_legacy, ref_v3, d):
    d = Path(d)
    for fname, v3 in (('rules.yaml', False), ('rules-v2.yaml', False), ('rules-v3.yaml', True)):
        p = d / fname
        if not p.is_file():
            print(f'SKIP FR-LC-03 {fname}: not found in {d}')
            continue
        text = p.read_text()
        names = [f'{a.upper()} {a} node' for a in real_aliases(text, v3)] or ['DE de node']
        case(f'FR-LC-03 real {fname}', ref_v3 if v3 else ref_legacy, sub_text(names), text,
             contract='3\n' if v3 else None)


# ---------------------------------------------------------------- FR-LC-04
def helpers_case(old_path):
    name = 'FR-LC-04 runpy helpers equal to r7'
    try:
        old, new = runpy.run_path(str(old_path)), runpy.run_path(str(BUILDER))
        why = []
        for sym in ('BUILTIN_POLICIES', '_parse_rules_file', '_rule_policy', '_unique_object'):
            if sym not in new:
                why.append(f'missing {sym}')
        if not why:
            if new['BUILTIN_POLICIES'] != old['BUILTIN_POLICIES']:
                why.append('BUILTIN_POLICIES differ')
            for rule in ['DOMAIN-SUFFIX,a.example,de', 'MATCH,DIRECT', 'GEOIP,RU,DIRECT,no-resolve',
                         'IP-CIDR,10.0.0.0/8,Telegram,no-resolve', 'DOMAIN,x.example,de-failover',
                         'DOMAIN-SUFFIX,a.example', '', 'AND,((DOMAIN,a),(NETWORK,UDP)),de']:
                outs = []
                for ns in (old, new):
                    try:
                        outs.append(('ok', ns['_rule_policy'](rule)))
                    except Exception as e:
                        outs.append(('exc', type(e).__name__))
                if outs[0] != outs[1]:
                    why.append(f'_rule_policy({rule!r}) {outs[0]} vs {outs[1]}')
            docs = {'plain': 'rules:\n  - "DOMAIN,a.example,de"\n  - MATCH,DIRECT\n',
                    'v2': rules_text(['DOMAIN,a,ee-failover'],
                                     'cascade-fallbacks: {"ee":["pl"]}\ncascade-probes: ["pl"]\n'),
                    'v3': rules_text(['DOMAIN,a,svc-de3'], V3_HEAD),
                    'empty': '', 'garbage': 'rules: [\n'}
            for dn, txt in docs.items():
                f = Path(tempfile.mkdtemp(prefix='lc-h-', dir=TMP)) / 'r.yaml'
                f.write_text(txt)
                outs = []
                for ns in (old, new):
                    try:
                        outs.append(('ok', repr(ns['_parse_rules_file'](f))))
                    except BaseException as e:
                        outs.append(('exc', type(e).__name__))
                if outs[0] != outs[1]:
                    why.append(f'_parse_rules_file({dn}) {outs[0]} vs {outs[1]}')
            for obj in ('{"a":1,"b":2}', '{"a":1,"a":2}', '[1]', '"s"'):
                outs = []
                for ns in (old, new):
                    try:
                        import json
                        outs.append(('ok', repr(json.loads(obj, object_pairs_hook=ns['_unique_object']))))
                    except BaseException as e:
                        outs.append(('exc', type(e).__name__))
                if outs[0] != outs[1]:
                    why.append(f'_unique_object({obj}) {outs[0]} vs {outs[1]}')
        why = '; '.join(why) or None
    except Exception as e:
        why = f'harness error: {e!r}'
    if why:
        FAILS.append(name)
    print(('FAIL ' if why else 'PASS ') + name + (f' - {why}' if why else ''), flush=True)


def main():
    global TMP
    ap = argparse.ArgumentParser()
    ap.add_argument('--real-rules-dir')
    args = ap.parse_args()
    TMP = tempfile.mkdtemp(prefix='legacy-compat-')
    try:
        ref_legacy, ref_v3 = git_show(LEGACY_REV), git_show(V3_REV)
        legacy_cases(ref_legacy)
        v3_cases(ref_v3)
        if args.real_rules_dir:
            real_cases(ref_legacy, ref_v3, args.real_rules_dir)
        helpers_case(ref_v3)
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    print(f'LEGACY-COMPAT: {len(FAILS)} failed')
    return 1 if FAILS else 0


if __name__ == '__main__':
    sys.exit(main())
