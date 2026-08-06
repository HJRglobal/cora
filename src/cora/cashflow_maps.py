"""Loaders for the 13WCF shadow-ledger maps (M2/S2).

Two data files, one job each:

  qbo-cashflow-entity-map.yaml    QBO realm  -> Standing ACTUALS tab
  qbo-cashflow-category-map.yaml  QBO account -> sheet category row

Both are seeded UNCONFIRMED and confirmed by Justin. This module is where the
fail-closed rules live, so that a bad edit fails at LOAD -- loudly, before any
figure is computed -- rather than quietly producing a wrong number:

  * `excluded_realms` is enforced HERE, not by absence. HRLLC (personal books)
    and OSN (cash-less shell) must raise if a future edit maps them, not
    silently work. What is never collected cannot leak.
  * A realm that could map to more than one tab must declare its split
    (`scope_attested` or `filters`) before it resolves. QBO exposes one LEX
    realm against five Lex tabs; guessing would attribute LBHS/LTS/LLA activity
    to LLC. Unresolvable -> UNKNOWN, never a guess.
  * No CC-liability account may map to an expense category. Card purchases are
    not cash events -- the bank->CC payment is -- so such a mapping would
    double-count every carded dollar.
  * LEX account names never leave this module for a shared surface; callers
    render `placeholder` (D-124).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]

ENTITY_MAP_PATH = _REPO_ROOT / "data" / "maps" / "qbo-cashflow-entity-map.yaml"
CATEGORY_MAP_PATH = _REPO_ROOT / "data" / "maps" / "qbo-cashflow-category-map.yaml"

#: Realms that must never be read, whatever the file says. The YAML carries the
#: same list; this is the belt that survives an edit to the file.
HARD_EXCLUDED_REALMS: frozenset[str] = frozenset({"HRLLC", "OSN"})

#: Account types that hold a credit-card LIABILITY rather than bank cash.
CC_LIABILITY_TYPES: frozenset[str] = frozenset({"credit card", "creditcard"})

#: Account types that hold bank cash -- the cash perimeter.
BANK_TYPES: frozenset[str] = frozenset({"bank"})

#: Realm PREFIXES whose account NAMES must never render on a shared surface.
#:
#: Prefix-matched, not exact (D-124 corollary): an exact-match gate would let a
#: future `LEX-LLC` or `LEX2` realm free-render the very names the gate exists to
#: hide, and the failure would be silent -- the guard would simply not fire.
#:
#: Note the asymmetry with HARD_EXCLUDED_REALMS below, which is deliberately
#: EXACT: prefix-excluding "OSN" would sweep away OSNGF/OSNGM/OSNGW/OSNVV, the
#: four operating store realms this extractor exists to read. Widening an opacity
#: gate is fail-safe; widening an exclusion list deletes real coverage.
NAME_OPAQUE_REALM_PREFIXES: tuple[str, ...] = ("LEX",)


def realm_names_are_opaque(realm: str) -> bool:
    """True when this realm's account names may not leave the module (D-124)."""
    code = str(realm or "").upper()
    return code.startswith(NAME_OPAQUE_REALM_PREFIXES)


class MapError(Exception):
    """A map file is unusable. Never fall back to a guess -- refuse."""


@dataclass
class RealmPairing:
    """How one QBO realm resolves (or refuses to resolve) onto a sheet tab."""
    realm: str
    tab: Optional[str] = None
    confirmed: bool = False
    confidence: str = ""
    scope_attested: bool = False
    filters: dict = field(default_factory=dict)
    candidate_tabs: list[str] = field(default_factory=list)
    note: str = ""

    @property
    def resolvable(self) -> bool:
        """True if S2 may compute figures for this realm at all.

        A single named tab resolves. An ambiguous realm resolves ONLY once its
        split is declared -- currently that means `scope_attested`: an attestation
        that the company file equals exactly the named tab.

        `filters` DOES NOT RESOLVE, and that is the fix for a D-051 HIGH. The
        first cut short-circuited `if self.filters: return True` BEFORE the tab
        check, while no consumer ever read `filters` -- so the moment Justin
        followed the instruction in the YAML and supplied account filters, the
        extractor would have read the ENTIRE realm unscoped and published it with
        `tab: None`. The mechanism advertised as the containment gate was inert,
        and using it OPENED the realm instead of narrowing it. Until per-tab
        splitting is implemented (see refusal_reason), filters refuse.
        """
        if not self.tab:
            return False
        if self.filters:
            return False
        if self.candidate_tabs and not self.scope_attested:
            return False
        return True

    @property
    def refusal_reason(self) -> str:
        if self.resolvable:
            return ""
        if self.filters:
            return (
                f"{self.realm} declares `filters`, which NOTHING APPLIES yet -- "
                "S2 reads a realm whole, so honouring this pairing would publish "
                "every sibling entity's activity under one tab. Splitting one "
                "realm across several tabs needs a per-tab schema (`tab_splits`) "
                "that does not exist yet. Use `scope_attested` if the company "
                "file really is exactly one tab; otherwise this realm stays "
                "UNKNOWN, which is the correct answer."
            )
        if self.candidate_tabs:
            return (
                f"{self.realm} could map to {len(self.candidate_tabs)} tabs "
                f"({', '.join(self.candidate_tabs)}) and its split is not declared "
                "-- needs a tab plus scope_attested"
            )
        return f"{self.realm} has no tab assigned"

    @property
    def usable_for_accuracy(self) -> bool:
        """Resolvable AND confirmed. An unconfirmed pair renders UNCONFIRMED
        (D-118) and never feeds comparison or accuracy math."""
        return self.resolvable and self.confirmed


@dataclass
class EntityMap:
    pairs: dict[str, RealmPairing] = field(default_factory=dict)
    excluded_realms: frozenset[str] = HARD_EXCLUDED_REALMS
    derived_tabs: list[str] = field(default_factory=list)
    manual_entry_tabs: list[str] = field(default_factory=list)

    def pairing(self, realm: str) -> Optional[RealmPairing]:
        return self.pairs.get(realm.upper())

    def is_excluded(self, realm: str) -> bool:
        return realm.upper() in self.excluded_realms

    def confirmed_count(self) -> int:
        return sum(1 for p in self.pairs.values() if p.usable_for_accuracy)

    def tab_is_covered(self, tab: str) -> bool:
        """True if some CONFIRMED, resolvable realm feeds this tab."""
        return any(p.tab == tab and p.usable_for_accuracy for p in self.pairs.values())

    def tab_status(self, tab: str) -> str:
        """How a downstream surface must label this tab (D-117)."""
        if tab in self.derived_tabs:
            return "derived-rollup"
        if tab in self.manual_entry_tabs:
            return "manual-entry (no QBO source)"
        if self.tab_is_covered(tab):
            return "qbo-covered"
        if any(p.tab == tab or tab in p.candidate_tabs for p in self.pairs.values()):
            return "awaiting-map-confirmation"
        return "manual-entry (no QBO source)"


def _read_yaml(path: Path) -> dict:
    if not path.exists():
        raise MapError(f"map file missing: {path.name}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise MapError(f"{path.name} is not parseable: {exc}") from exc
    if not isinstance(data, dict):
        raise MapError(f"{path.name} did not parse to a mapping")
    return data


def load_entity_map(path: Optional[Path] = None) -> EntityMap:
    """Load and VALIDATE the realm<->tab map.

    Raises MapError on any condition where continuing would risk a wrong
    attribution. There is no permissive mode.
    """
    data = _read_yaml(path or ENTITY_MAP_PATH)

    declared = {str(r).upper() for r in (data.get("excluded_realms") or [])}
    missing = HARD_EXCLUDED_REALMS - declared
    if missing:
        raise MapError(
            f"excluded_realms is missing hard exclusion(s): {sorted(missing)}. "
            "HRLLC is personal books and OSN is a cash-less shell; both must stay "
            "excluded at collection."
        )
    excluded = frozenset(declared | HARD_EXCLUDED_REALMS)

    raw_pairs = data.get("pairs") or {}
    if not isinstance(raw_pairs, dict):
        raise MapError("`pairs` must be a mapping of realm -> pairing")

    pairs: dict[str, RealmPairing] = {}
    for realm, body in raw_pairs.items():
        code = str(realm).upper()
        if code in excluded:
            # Loud, not silent: a mapped-but-excluded realm means someone
            # intended to read personal or shell books.
            raise MapError(
                f"realm {code} is in excluded_realms but also has a pairing -- "
                "remove the pairing or the exclusion, do not have both"
            )
        if not isinstance(body, dict):
            raise MapError(f"pairing for {code} must be a mapping")
        tab = body.get("tab")
        pairs[code] = RealmPairing(
            realm=code,
            tab=str(tab) if tab else None,
            confirmed=bool(body.get("confirmed", False)),
            confidence=str(body.get("confidence") or ""),
            scope_attested=bool(body.get("scope_attested", False)),
            filters=dict(body.get("filters") or {}),
            candidate_tabs=[str(t) for t in (body.get("candidate_tabs") or [])],
            note=str(body.get("note") or ""),
        )

    # A confirmed pairing that does not resolve is a contradiction -- somebody
    # ticked the box without declaring the split.
    for p in pairs.values():
        if p.confirmed and not p.resolvable:
            raise MapError(
                f"realm {p.realm} is marked confirmed but does not resolve: "
                f"{p.refusal_reason}"
            )

    # Two confirmed realms feeding one tab would double-count it.
    seen: dict[str, str] = {}
    for p in pairs.values():
        if not (p.tab and p.usable_for_accuracy):
            continue
        if p.tab in seen:
            raise MapError(
                f"tab {p.tab!r} is claimed by two confirmed realms "
                f"({seen[p.tab]} and {p.realm}) -- its actuals would double-count"
            )
        seen[p.tab] = p.realm

    return EntityMap(
        pairs=pairs,
        excluded_realms=excluded,
        derived_tabs=[str(t) for t in (data.get("derived_tabs") or [])],
        manual_entry_tabs=[str(t) for t in (data.get("manual_entry_tabs") or [])],
    )


@dataclass
class AccountMapping:
    account_id: str
    realm: str
    account_type: str
    category: Optional[str] = None
    qbo_account: str = ""
    placeholder: str = ""
    confidence: str = ""
    confirmed: bool = False

    @property
    def is_bank(self) -> bool:
        return self.account_type.strip().lower() in BANK_TYPES

    @property
    def is_cc_liability(self) -> bool:
        return self.account_type.strip().lower().replace(" ", "") in {
            t.replace(" ", "") for t in CC_LIABILITY_TYPES
        }

    def display_name(self) -> str:
        """What a SHARED surface may render.

        LEX account names never leave this module (D-124); a LEX row without a
        placeholder renders an opaque fallback rather than falling back to the
        real name.
        """
        if realm_names_are_opaque(self.realm):
            return self.placeholder or f"{self.realm} account {self.account_id}"
        return self.qbo_account or f"account {self.account_id}"


@dataclass
class CategoryMap:
    categories: dict[str, list[str]] = field(default_factory=dict)
    expense_categories: frozenset[str] = frozenset()
    accounts: dict[tuple[str, str], AccountMapping] = field(default_factory=dict)
    transaction_types: dict[str, str] = field(default_factory=dict)

    def all_category_rows(self) -> set[str]:
        return {row for rows in self.categories.values() for row in rows}

    def mapping(self, realm: str, account_id: str) -> Optional[AccountMapping]:
        return self.accounts.get((realm.upper(), str(account_id)))

    def category_for(self, realm: str, account_id: str) -> Optional[str]:
        """The confirmed category row, or None.

        An UNCONFIRMED mapping returns None on purpose: the transaction lands in
        `uncategorized` and is reported, rather than being placed on a row
        nobody has verified.
        """
        m = self.mapping(realm, account_id)
        return m.category if (m and m.confirmed and m.category) else None


def load_category_map(path: Optional[Path] = None) -> CategoryMap:
    """Load and VALIDATE the account->category map."""
    data = _read_yaml(path or CATEGORY_MAP_PATH)

    categories = {
        str(group): [str(r) for r in (rows or [])]
        for group, rows in (data.get("categories") or {}).items()
    }
    if not categories:
        raise MapError("`categories` is empty -- there is nothing to map onto")
    valid_rows = {row for rows in categories.values() for row in rows}

    expense = frozenset(str(c) for c in (data.get("expense_categories") or []))
    unknown_expense = expense - valid_rows
    if unknown_expense:
        raise MapError(
            f"expense_categories names row(s) that are not in categories: "
            f"{sorted(unknown_expense)}"
        )

    accounts: dict[tuple[str, str], AccountMapping] = {}
    for realm, body in (data.get("realms") or {}).items():
        code = str(realm).upper()
        if code in HARD_EXCLUDED_REALMS:
            raise MapError(
                f"realm {code} is hard-excluded but has category mappings -- "
                "remove them"
            )
        for acct_id, entry in ((body or {}).get("accounts") or {}).items():
            entry = entry or {}
            m = AccountMapping(
                account_id=str(acct_id),
                realm=code,
                account_type=str(entry.get("account_type") or ""),
                category=(str(entry["category"]) if entry.get("category") else None),
                qbo_account=str(entry.get("qbo_account") or ""),
                placeholder=str(entry.get("placeholder") or ""),
                confidence=str(entry.get("confidence") or ""),
                confirmed=bool(entry.get("confirmed", False)),
            )
            if m.category and m.category not in valid_rows:
                raise MapError(
                    f"{code} account {acct_id} maps to unknown category "
                    f"{m.category!r}"
                )
            # THE CASH-PERIMETER ASSERTION (Fin-3). A card purchase is not a cash
            # event; the bank->CC payment is. Mapping a CC-liability account to an
            # expense row would double-count every carded dollar.
            if m.is_cc_liability and m.category in expense:
                raise MapError(
                    f"{code} account {acct_id} is a Credit Card liability mapped to "
                    f"expense category {m.category!r}. Card purchases are not cash "
                    "events -- the bank-to-card PAYMENT is. This mapping would "
                    "double-count carded spend."
                )
            # THE SYMMETRIC HALF. A BANK account is the perimeter, never a
            # category: it appears as a transaction's counterpart only when money
            # moved between two of our own accounts, which `split_gross` keeps out
            # of receipts and disbursements precisely because it is neither. One
            # hand-added row here would file a $150K internal sweep as both income
            # and spend in the same week -- inflating both sides while net_flow
            # stayed correct, the same shape as the bug split_gross exists to
            # prevent. Discovery already refuses to propose these; this stops a
            # hand edit too.
            if m.is_bank and m.category:
                raise MapError(
                    f"{code} account {acct_id} is a BANK account mapped to category "
                    f"{m.category!r}. A bank account is the cash perimeter, not a "
                    "category -- it shows up as a counterpart only for internal "
                    "transfers, which are neither receipts nor disbursements."
                )
            accounts[(code, str(acct_id))] = m

    return CategoryMap(
        categories=categories,
        expense_categories=expense,
        accounts=accounts,
        transaction_types={
            str(k): str(v) for k, v in (data.get("transaction_types") or {}).items()
        },
    )
