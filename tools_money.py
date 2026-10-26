"""Yandex Direct money tools: bidding strategies (own + package), daily budget, account funds.

Закрывает дыру форка: штатный campaigns.get не запрашивает типовые поля кампании,
поэтому стратегия не видна вообще, а сервиса strategies (пакетные стратегии) нет.

Контракт тот же, что у tools_direct_extra: async handler(client, args, config).
Все суммы в аргументах и в ответах — В РУБЛЯХ; в API уходят микроединицы (×1 000 000).
"""

import re
import json
import logging

from mcp.types import Tool

logger = logging.getLogger("yandex-ads-mcp")


# ── связь с tools_direct_extra ─────────────────────────────────────────
# tools_direct_extra подключает нас в самом конце себя, поэтому импорт кольцевой.
# На уровне модуля он работал бы только при одном порядке импорта (сначала
# tools_direct_extra) и падал бы при обратном. Поэтому берём зависимости лениво,
# в момент вызова, когда оба модуля уже загружены.

def _api(*args, **kwargs):
    import tools_direct_extra as _tde
    return _tde._api(*args, **kwargs)  # корутина — вызывающий её await'ит


def _cfg(config):
    import tools_direct_extra as _tde
    return _tde._cfg(config)


def _result(data):
    import tools_direct_extra as _tde
    return _tde._result(data)


def _eff_login(c):
    """Логин, под которым реально уйдёт запрос (per-call Client-Login важнее глобального)."""
    import tools_direct_extra as _tde
    return _tde.client_login_var.get("") or c.get("login") or ""

MICRO = 1_000_000

PACKAGE_HINT = ("Кампании в пакетной стратегии нельзя править по одной (ошибка 5005) — "
                "меняй пакет через yandex_direct_strategies_update по StrategyId.")


# ── деньги ─────────────────────────────────────────────────────────────

def _rub(micro):
    if not isinstance(micro, (int, float)):
        return micro
    return round(micro / MICRO, 2)


def _mic(rubles):
    if rubles is None:
        return None
    return int(round(float(rubles) * MICRO))


_MONEY_KEYS = {
    "WeeklySpendLimit", "BidCeiling", "AverageCpc", "AverageCpa", "Cpa",
    "SpendLimit", "MinimumExplorationBudget", "Amount", "Sum", "Balance",
    "BalanceBonus", "SumAvailableForTransfer", "AverageCpm", "OverdraftSumAvailable",
}


def _humanize_goals(pg):
    """PriorityGoals: Value — это ценность цели в микроединицах (1000000000 = 1000 ₽)."""
    if not isinstance(pg, dict):
        return pg
    items = []
    for it in (pg.get("Items") or []):
        it = dict(it)
        if isinstance(it.get("Value"), (int, float)):
            it["Value"] = _rub(it["Value"])
        items.append(it)
    return dict(pg, Items=items)


def _humanize(obj):
    """Рекурсивно переводит известные денежные поля из микроединиц в рубли."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k == "PriorityGoals":
                out[k] = _humanize_goals(v)
            elif k in _MONEY_KEYS and isinstance(v, (int, float)):
                out[k] = _rub(v)
            else:
                out[k] = _humanize(v)
        return out
    if isinstance(obj, list):
        return [_humanize(v) for v in obj]
    return obj


def _err_code(exc):
    """Форк заворачивает ошибку API в Exception('API error <code>: ...') — достаём код."""
    m = re.search(r"API error (\d+)", str(exc))
    return int(m.group(1)) if m else None


# ── описание стратегий ─────────────────────────────────────────────────

# ключ = имя объекта в запросе/ответе; fn = имя блока *FieldNames в strategies.get
STRATEGY_BLOCKS = {
    "WbMaximumClicks": {
        "fn": "StrategyMaximumClicksFieldNames",
        "fields": ["WeeklySpendLimit", "BidCeiling", "CustomPeriodBudget", "BudgetType"],
        "required": ["WeeklySpendLimit"],
    },
    "WbMaximumConversionRate": {
        "fn": "StrategyMaximumConversionRateFieldNames",
        "fields": ["WeeklySpendLimit", "BidCeiling", "GoalId", "CustomPeriodBudget", "BudgetType"],
        "required": ["WeeklySpendLimit", "GoalId"],
    },
    "AverageCpc": {
        "fn": "StrategyAverageCpcFieldNames",
        "fields": ["AverageCpc", "WeeklySpendLimit", "CustomPeriodBudget", "BudgetType"],
        "required": ["AverageCpc"],
    },
    "AverageCpa": {
        "fn": "StrategyAverageCpaFieldNames",
        "fields": ["AverageCpa", "GoalId", "WeeklySpendLimit", "BidCeiling",
                   "CustomPeriodBudget", "BudgetType", "ExplorationBudget"],
        "required": ["AverageCpa", "GoalId"],
    },
    "AverageCpaMultipleGoals": {
        "fn": "StrategyAverageCpaMultipleGoalsFieldNames",
        "fields": ["WeeklySpendLimit", "BidCeiling", "CustomPeriodBudget", "BudgetType", "ExplorationBudget", "PriorityGoals"],
        "required": [],
    },
    "PayForConversion": {
        "fn": "StrategyPayForConversionFieldNames",
        "fields": ["Cpa", "GoalId", "WeeklySpendLimit", "CustomPeriodBudget", "BudgetType"],
        "required": ["Cpa", "GoalId"],
    },
    "PayForConversionMultipleGoals": {
        "fn": "StrategyPayForConversionMultipleGoalsFieldNames",
        "fields": ["WeeklySpendLimit", "CustomPeriodBudget", "BudgetType", "PriorityGoals"],
        "required": [],
    },
    "AverageCrr": {
        "fn": "StrategyAverageCrrFieldNames",
        "fields": ["Crr", "GoalId", "WeeklySpendLimit", "CustomPeriodBudget", "BudgetType", "ExplorationBudget"],
        "required": ["Crr", "GoalId"],
    },
    "PayForConversionCrr": {
        "fn": "StrategyPayForConversionCrrFieldNames",
        "fields": ["Crr", "GoalId", "WeeklySpendLimit", "CustomPeriodBudget", "BudgetType"],
        "required": ["Crr", "GoalId"],
    },
    "MaxProfit": {
        "fn": "StrategyMaxProfitFieldNames",
        "fields": ["WeeklySpendLimit", "CustomPeriodBudget", "BudgetType", "ExplorationBudget"],
        "required": [],
    },
    "HighestPosition": {
        "fn": "StrategyHighestPositionFieldNames",
        "fields": ["WeeklySpendLimit"],
        "required": [],
    },
    "ManualCpm": {
        "fn": "StrategyManualCpmFieldNames",
        "fields": ["WeeklySpendLimit"],
        "required": ["WeeklySpendLimit"],
    },
}

# тип стратегии (BiddingStrategyType) -> имя объекта
TYPE_TO_BLOCK = {
    "WB_MAXIMUM_CLICKS": "WbMaximumClicks",
    "WB_MAXIMUM_CONVERSION_RATE": "WbMaximumConversionRate",
    "AVERAGE_CPC": "AverageCpc",
    "AVERAGE_CPA": "AverageCpa",
    "AVERAGE_CRR": "AverageCrr",
    "PAY_FOR_CONVERSION": "PayForConversion",
    "PAY_FOR_CONVERSION_CRR": "PayForConversionCrr",
    "AVERAGE_CPA_MULTIPLE_GOALS": "AverageCpaMultipleGoals",
    "PAY_FOR_CONVERSION_MULTIPLE_GOALS": "PayForConversionMultipleGoals",
    "HIGHEST_POSITION": "HighestPosition",
    "NETWORK_DEFAULT": "NetworkDefault",
    "MAXIMUM_COVERAGE": None,
    "SERVING_OFF": None,
}

SEARCH_TYPES = ["HIGHEST_POSITION", "WB_MAXIMUM_CLICKS", "WB_MAXIMUM_CONVERSION_RATE",
                "AVERAGE_CPC", "AVERAGE_CPA", "AVERAGE_CRR", "PAY_FOR_CONVERSION",
                "PAY_FOR_CONVERSION_CRR",
                "AVERAGE_CPA_MULTIPLE_GOALS", "PAY_FOR_CONVERSION_MULTIPLE_GOALS", "SERVING_OFF"]
NETWORK_TYPES = ["NETWORK_DEFAULT", "MAXIMUM_COVERAGE", "WB_MAXIMUM_CLICKS",
                 "WB_MAXIMUM_CONVERSION_RATE", "AVERAGE_CPC", "AVERAGE_CPA",
                 "AVERAGE_CRR", "PAY_FOR_CONVERSION", "PAY_FOR_CONVERSION_CRR",
                 "AVERAGE_CPA_MULTIPLE_GOALS", "PAY_FOR_CONVERSION_MULTIPLE_GOALS", "SERVING_OFF"]

STRATEGY_COMMON_FIELDS = ["Id", "Name", "Type", "StatusArchived", "AttributionModel",
                          "CounterIds", "PriorityGoals"]

CAMPAIGN_COMMON_FIELDS = ["Id", "Name", "Type", "State", "Status", "StatusPayment",
                          "DailyBudget", "Funds", "Currency", "StartDate", "EndDate"]
CAMPAIGN_TYPED_FIELDS = ["BiddingStrategy", "PackageBiddingStrategy",
                         "CanBeUsedAsPackageBiddingStrategySource", "PriorityGoals",
                         "AttributionModel", "CounterIds"]
TYPED_FN = [
    "TextCampaignFieldNames",
    "UnifiedCampaignFieldNames",
    "DynamicTextCampaignFieldNames",
    "SmartCampaignFieldNames",
    "MobileAppCampaignFieldNames",
    "CpmBannerCampaignFieldNames",
]

# коды, на которых имеет смысл повторить запрос с урезанным набором полей
_DEGRADE_CODES = {4003, 8800, 5005, 3000, 3001, 5001, 8000}


def _degrade(blocks, exc):
    """Чинит набор *FieldNames по подсказке из ошибки Директа.

    Аккаунты отличаются набором поддерживаемых полей и подтипов кампаний, поэтому
    запрос иногда приходится ужимать. Директ в тексте ошибки сам говорит, что не так:
      - «Указан неизвестный параметр X» — блок X убираем целиком;
      - «Элемент массива X содержит неверное значение …, ожидается одно из значений: …»
        — оставляем в X только перечисленные поля (блок целиком не теряем).
    Возвращает новый список блоков либо None, если чинить нечего — тогда ошибка летит выше.
    """
    if not blocks or _err_code(exc) not in _DEGRADE_CODES:
        return None
    text = str(exc)

    m = re.search(r"(?:Элемент массива|Array element)\s+(\w+).*?"
                  r"(?:ожидается одно из значений|expected one of)[:\s]+([\w,\s]+)", text, re.I | re.S)
    if m:
        target, allowed_raw = m.group(1), m.group(2)
        allowed = {a.strip() for a in allowed_raw.split(",") if a.strip()}
        out = []
        for b in blocks:
            if b["fn"] != target:
                out.append(b)
                continue
            kept = [f for f in b["fields"] if f in allowed]
            if kept and kept != b["fields"]:
                logger.warning("сужаю %s до %s", target, ", ".join(kept))
                out.append({**b, "fields": kept})
            else:
                logger.warning("убираю блок %s (нечего оставить)", target)
        if out != blocks:
            return out

    m = re.search(r"(?:неизвестн\w+ параметр|unknown parameter)[:\s]+(\w+)", text, re.I)
    if m:
        pruned = [b for b in blocks if b["fn"] != m.group(1)]
        if pruned != blocks:
            logger.warning("убираю блок %s (не поддерживается аккаунтом)", m.group(1))
            return pruned

    logger.warning("убираю блок %s вслепую (%s)", blocks[-1]["fn"], text[:120])
    return blocks[:-1]


# Подобранный набор *FieldNames переживает вызовы: набор зависит от аккаунта, а не от
# запроса, поэтому платить за подбор баллами API каждый раз не нужно. Ключ — (что читаем, логин).
_FIELD_CACHE = {}


async def _get_degrading(client, c, cache_key, full_blocks, service, base_params, result_key):
    """GET с подбором *FieldNames: сначала кешированный набор, при ошибке — чиним по подсказке."""
    key = (cache_key, _eff_login(c))
    from_cache = key in _FIELD_CACHE
    blocks = [dict(b) for b in _FIELD_CACHE.get(key, full_blocks)]
    while True:
        params = dict(base_params)
        for b in blocks:
            params[b["fn"]] = list(b["fields"])
        try:
            data = await _api(client, service, "get", params, **c)
            _FIELD_CACHE[key] = [dict(b) for b in blocks]
            return (data.get("result") or {}).get(result_key) or []
        except Exception as e:
            nxt = _degrade(blocks, e)
            if nxt is None:
                if from_cache:  # кеш устарел — пробуем заново с полного набора
                    logger.warning("сбрасываю кеш полей %s", key)
                    _FIELD_CACHE.pop(key, None)
                    from_cache = False
                    blocks = [dict(b) for b in full_blocks]
                    continue
                raise
            blocks = nxt


_UNKNOWN_FIELD_RE = re.compile(r"(?:неизвестн\w+ параметр|unknown parameter)\s+([\w.]+)", re.I)


def _prune_path(payload, path):
    """Удаляет поле по пути вида Block.Field где угодно в теле запроса. True — если удалил."""
    parts = path.split(".")
    field = parts[-1]
    parent = parts[-2] if len(parts) > 1 else None
    removed = False

    def walk(node):
        nonlocal removed
        if isinstance(node, dict):
            for k, v in list(node.items()):
                if isinstance(v, dict) and field in v and (parent is None or k == parent):
                    v.pop(field)
                    removed = True
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(payload)
    return removed


async def _api_write(client, c, service, method, params, max_prunes=8):
    """Пишущий вызов, который сам снимает поля, не принимаемые этим методом.

    Директ отдаёт в get больше полей, чем принимает в update (например
    PayForConversionMultipleGoals.BudgetType), а мы переносим текущие настройки
    целиком, чтобы не обнулить их. Расхождения снимаем по подсказке из ошибки.
    """
    for _ in range(max_prunes + 1):
        try:
            return await _api(client, service, method, params, **c)
        except Exception as e:
            m = _UNKNOWN_FIELD_RE.search(str(e))
            if not m or not _prune_path(params, m.group(1)):
                raise
            logger.warning("убираю поле %s — %s не принимает его", m.group(1), method)
    raise RuntimeError("не удалось подобрать состав полей для %s.%s" % (service, method))


# ── чтение ─────────────────────────────────────────────────────────────

async def _strategies_get_raw(client, c, ids=None, types=None, archived=None):
    """strategies.get с деградацией: аккаунт не переварил блок *FieldNames — убираем его."""
    selection = {}
    if ids:
        selection["Ids"] = list(ids)
    if types:
        selection["Types"] = list(types)
    if archived is not None:
        selection["IsArchived"] = "YES" if archived else "NO"
    if not selection:
        # strategies.get возвращает 8000 без SelectionCriteria — по умолчанию берём живые
        selection["IsArchived"] = "NO"

    full = [{"fn": b["fn"], "fields": list(b["fields"])} for b in STRATEGY_BLOCKS.values()]
    return await _get_degrading(
        client, c, "strategies", full, "strategies",
        {"FieldNames": list(STRATEGY_COMMON_FIELDS), "SelectionCriteria": selection}, "Strategies")


async def _campaigns_money_raw(client, c, ids=None, states=None):
    """campaigns.get с типовыми полями (BiddingStrategy/PackageBiddingStrategy) и деградацией."""
    selection = {}
    if ids:
        selection["Ids"] = list(ids)
    if states:
        selection["States"] = list(states)

    full = [{"fn": fn, "fields": list(CAMPAIGN_TYPED_FIELDS)} for fn in TYPED_FN]
    return await _get_degrading(
        client, c, "campaigns", full, "campaigns",
        {"FieldNames": list(CAMPAIGN_COMMON_FIELDS), "SelectionCriteria": selection}, "Campaigns")


def _flatten(camp):
    """Поднимает стратегию из типового подобъекта (TextCampaign/UnifiedCampaign/…) наверх."""
    out = {k: camp.get(k) for k in CAMPAIGN_COMMON_FIELDS if k in camp}
    for key, val in camp.items():
        if key.endswith("Campaign") and isinstance(val, dict):
            out["CampaignSubtype"] = key
            for f in CAMPAIGN_TYPED_FIELDS:
                if f in val:
                    out[f] = val[f]
    return out


def _package_id(flat):
    pkg = flat.get("PackageBiddingStrategy")
    if isinstance(pkg, dict) and pkg.get("StrategyId"):
        return pkg["StrategyId"]
    return None


def _summarize(flat):
    """Однострочная сводка стратегии кампании — то, что раньше не было видно вообще."""
    pid = _package_id(flat)
    if pid:
        return f"ПАКЕТНАЯ стратегия #{pid} (правится через yandex_direct_strategies_update)"
    bs = flat.get("BiddingStrategy") or {}
    parts = []
    for side, label in (("Search", "поиск"), ("Network", "сети")):
        blk = bs.get(side) or {}
        t = blk.get("BiddingStrategyType")
        if not t:
            continue
        det = []
        for key, obj in blk.items():
            if key == "BiddingStrategyType" or not isinstance(obj, dict):
                continue
            for f in ("AverageCpa", "Cpa", "AverageCpc", "BidCeiling", "WeeklySpendLimit"):
                if obj.get(f) is not None:
                    det.append("%s=%s₽" % (f, _rub(obj[f])))
            for f in ("Crr", "GoalId", "LimitPercent", "BudgetIncreasePercent"):
                if obj.get(f) is not None:
                    det.append("%s=%s" % (f, obj[f]))
        parts.append("%s: %s%s" % (label, t, (" (" + ", ".join(det) + ")") if det else ""))
    return "; ".join(parts) or "стратегия не отдана API"


def _build_priority_goals(args, current):
    """Ставки за конверсию по целям. Незатронутые цели сохраняются как есть.

    Директ требует PriorityGoals для multiple-goals стратегий, поэтому при любой правке
    такой стратегии их надо переслать целиком — иначе ценности целей потеряются.
    """
    items = [dict(it) for it in ((current or {}).get("Items") or [])]
    wanted = args.get("priority_goals")
    if wanted:
        by_id = {int(it["GoalId"]): it for it in items}
        for w in wanted:
            gid = int(w["goal_id"])
            if float(w["value"]) <= 0:
                by_id.pop(gid, None)          # value <= 0 — УБРАТЬ цель из стратегии
                continue
            val = _mic(w["value"])
            if gid in by_id:
                by_id[gid]["Value"] = val
            else:
                by_id[gid] = {"GoalId": gid, "Value": val, "IsMetrikaSourceOfValue": "NO"}
        items = list(by_id.values())
    elif args.get("goal_value") is not None:
        # одна ставка на все текущие цели
        for it in items:
            it["Value"] = _mic(args["goal_value"])
    return {"Items": items} if items else None


def _detect_block(strategy):
    for key in STRATEGY_BLOCKS:
        if isinstance(strategy.get(key), dict):
            return key
    return None


# ── сборка объекта стратегии ───────────────────────────────────────────

def _build_block(kind, args, current=None):
    """Собирает объект стратегии, ДОБИРАЯ недостающее из текущих настроек.

    Именно поэтому правка CPA больше не сносит недельный бюджет: всё, что не задано
    аргументом, берётся из того, что уже стоит в кампании/пакете.
    """
    spec = STRATEGY_BLOCKS.get(kind)
    if not spec:
        raise ValueError("Неизвестный тип стратегии %s. Допустимо: %s" % (kind, ", ".join(STRATEGY_BLOCKS)))

    allowed = set(spec["fields"])
    cur = dict(current or {})
    obj = {}

    def put(field, value, money):
        if field not in allowed:
            return
        if value is not None:
            obj[field] = _mic(value) if money else value
        elif cur.get(field) is not None:
            obj[field] = cur[field]  # уже в микроединицах — как пришло из API

    put("WeeklySpendLimit", args.get("weekly_spend_limit"), True)
    put("BidCeiling", args.get("bid_ceiling"), True)
    put("AverageCpc", args.get("average_cpc"), True)
    put("AverageCpa", args.get("cpa"), True)
    put("Cpa", args.get("cpa"), True)
    put("Crr", args.get("crr"), False)
    put("GoalId", args.get("goal_id"), False)

    # Всё остальное, что уже стоит, переносим как есть: иначе правка одного CPA
    # уронила бы, например, BudgetType=CUSTOM_PERIOD_BUDGET вместе с CustomPeriodBudget
    # и молча превратила бюджет на период в недельный.
    # ExplorationBudget не переносим: Директ принимает его только с
    # IsMinimumExplorationBudgetCustom=YES, а у автоматического стоит NO.
    for f in spec["fields"]:
        if f not in obj and f != "ExplorationBudget" and cur.get(f) is not None:
            obj[f] = cur[f]

    missing = [f for f in spec["required"] if obj.get(f) is None]
    if missing:
        raise ValueError(
            "Для %s обязательны поля %s — задай их аргументами "
            "(weekly_spend_limit / cpa / average_cpc / crr / goal_id)." % (kind, ", ".join(missing))
        )
    if "PriorityGoals" in allowed:
        pg = _build_priority_goals(args, cur.get("PriorityGoals"))
        if pg:
            obj["PriorityGoals"] = pg

    return obj


def _build_side(side_type, args, current_side):
    """Собирает Search- или Network-блок BiddingStrategy."""
    cur = dict(current_side or {})
    t = side_type or cur.get("BiddingStrategyType")
    if not t:
        raise ValueError("Не задан тип стратегии и текущий неизвестен.")
    block = {"BiddingStrategyType": t}

    if t == "NETWORK_DEFAULT":
        cur_nd = cur.get("NetworkDefault") or {}
        lp = args.get("network_limit_percent")
        if lp is None:
            lp = cur_nd.get("LimitPercent")
        if lp is not None:
            block["NetworkDefault"] = {"LimitPercent": lp}
        return block

    kind = TYPE_TO_BLOCK.get(t)
    if kind:
        block[kind] = _build_block(kind, args, cur.get(kind) or {})
    return block


# ── хендлеры: чтение ───────────────────────────────────────────────────

async def _handle_money_overview(client, args, config):
    c = _cfg(config)
    camps = await _campaigns_money_raw(
        client, c, args.get("ids"), args.get("states") or ["ON", "SUSPENDED"])
    flats = [_flatten(x) for x in camps]

    pkg_ids = sorted({p for p in (_package_id(f) for f in flats) if p})
    packages = {}
    if pkg_ids:
        try:
            for s in await _strategies_get_raw(client, c, ids=pkg_ids):
                packages[str(s.get("Id"))] = _humanize(s)
        except Exception as e:
            packages = {"error": str(e)[:300]}

    rows = []
    for f in flats:
        row = {
            "Id": f.get("Id"),
            "Name": f.get("Name"),
            "State": f.get("State"),
            "Strategy": _summarize(f),
            "PackageStrategyId": _package_id(f),
            "DailyBudget": _humanize(f.get("DailyBudget")),
        }
        if args.get("raw"):
            row["BiddingStrategy"] = _humanize(f.get("BiddingStrategy"))
            row["Funds"] = _humanize(f.get("Funds"))
        rows.append(row)

    return _result({"Campaigns": rows, "PackageStrategies": packages, "Note": PACKAGE_HINT})


async def _handle_strategies_get(client, args, config):
    c = _cfg(config)
    strategies = await _strategies_get_raw(
        client, c, args.get("ids"), args.get("types"), args.get("archived"))
    return _result({"Strategies": [_humanize(s) for s in strategies], "Count": len(strategies)})


async def _handle_account_money(client, args, config):
    c = _cfg(config)
    data = await _api(client, "clients", "get", {
        "FieldNames": ["ClientId", "Login", "Currency", "OverdraftSumAvailable",
                       "Restrictions", "Settings", "Type", "VatRate", "AccountQuality"],
    }, **c)
    return _result(_humanize(data.get("result", data)))


# ── хендлеры: запись ───────────────────────────────────────────────────

async def _handle_strategies_update(client, args, config):
    """Правка ПАКЕТНОЙ стратегии — единственный способ поменять деньги кампании из пакета."""
    c = _cfg(config)
    sid = int(args["strategy_id"])
    found = await _strategies_get_raw(client, c, ids=[sid])
    if not found:
        raise ValueError("Пакетная стратегия %s не найдена в аккаунте." % sid)
    current = found[0]

    kind = args.get("strategy_kind") or _detect_block(current)
    if not kind:
        raise ValueError(
            "Не удалось определить тип стратегии %s (Type=%s). Передай strategy_kind явно: %s"
            % (sid, current.get("Type"), ", ".join(STRATEGY_BLOCKS)))

    item = {
        "Id": sid,
        "Name": args.get("name") or current.get("Name"),
        kind: _build_block(kind, args, current.get(kind) or {}),
    }
    # PriorityGoals для multiple-goals стратегий обязательны, а остальное лучше переслать
    # как есть: правка одной ставки не должна ронять ценности целей и счётчики.
    pg = _build_priority_goals(args, current.get("PriorityGoals"))
    if pg:
        item["PriorityGoals"] = pg
    for f in ("AttributionModel", "CounterIds"):
        if current.get(f) is not None:
            item[f] = current[f]
    data = await _api_write(client, c, "strategies", "update", {"Strategies": [item]})
    after = await _strategies_get_raw(client, c, ids=[sid])
    return _result({
        "Result": data.get("result", data),
        "Before": _humanize(current),
        "After": _humanize(after[0]) if after else None,
    })


async def _handle_strategy_set(client, args, config):
    """Стратегия ОДНОЙ кампании. Незаданные параметры наследуются, а не обнуляются."""
    c = _cfg(config)
    cid = int(args["campaign_id"])
    camps = await _campaigns_money_raw(client, c, ids=[cid])
    if not camps:
        raise ValueError(
            "Кампания %s не отдаётся campaigns.get. Если это Мастер кампаний — её настройки "
            "через API недоступны вообще, только интерфейс Директа." % cid)
    flat = _flatten(camps[0])

    pid = _package_id(flat)
    if pid:
        raise ValueError(
            "Кампания %s входит в ПАКЕТНУЮ стратегию #%s. Меняй через "
            "yandex_direct_strategies_update(strategy_id=%s) — правка применится ко всем "
            "кампаниям пакета." % (cid, pid, pid))

    subtype = flat.get("CampaignSubtype") or "TextCampaign"
    current_bs = flat.get("BiddingStrategy") or {}

    # алиасы под старый тул yd_campaign_strategy_update
    search_type = args.get("search_type") or args.get("strategy_search")
    network_type = args.get("network_type") or args.get("strategy_network")

    bs = {
        "Search": _build_side(search_type, args, current_bs.get("Search")),
        "Network": _build_side(network_type, args, current_bs.get("Network")),
    }
    item = {"Id": cid, subtype: {"BiddingStrategy": bs}}
    try:
        data = await _api_write(client, c, "campaigns", "update", {"Campaigns": [item]})
    except Exception as e:
        if _err_code(e) == 5005:
            raise ValueError(
                "5005 по кампании %s: почти всегда это пакетная стратегия или Мастер кампаний. "
                "Проверь yandex_direct_money_overview." % cid)
        raise

    after = await _campaigns_money_raw(client, c, ids=[cid])
    return _result({
        "Result": data.get("result", data),
        "Before": _summarize(flat),
        "After": _summarize(_flatten(after[0])) if after else None,
    })


async def _handle_daily_budget_set(client, args, config):
    c = _cfg(config)
    cid = int(args["campaign_id"])
    amount = args.get("amount")
    if amount and float(amount) > 0:
        item = {"Id": cid, "DailyBudget": {"Amount": _mic(amount),
                                           "Mode": args.get("mode") or "STANDARD"}}
    else:
        item = {"Id": cid, "DailyBudget": None}
    try:
        data = await _api_write(client, c, "campaigns", "update", {"Campaigns": [item]})
    except Exception as e:
        if _err_code(e) == 5005:
            raise ValueError(
                "5005 по кампании %s — дневной бюджет заблокирован пакетной стратегией "
                "или это Мастер кампаний; правится только в интерфейсе." % cid)
        raise
    after = await _campaigns_money_raw(client, c, ids=[cid])
    return _result({
        "Result": data.get("result", data),
        "DailyBudget": _humanize(_flatten(after[0]).get("DailyBudget")) if after else None,
    })


async def _handle_package_strategy_link(client, args, config):
    c = _cfg(config)
    cid = int(args["campaign_id"])
    sid = args.get("strategy_id")
    from_cid = args.get("strategy_from_campaign_id")
    if bool(sid) == bool(from_cid):
        raise ValueError("Задай ровно одно: strategy_id ИЛИ strategy_from_campaign_id.")

    camps = await _campaigns_money_raw(client, c, ids=[cid])
    if not camps:
        raise ValueError("Кампания %s не найдена через campaigns.get." % cid)
    subtype = _flatten(camps[0]).get("CampaignSubtype") or "TextCampaign"

    platforms = {
        "SearchResult": "YES" if args.get("search_result", True) else "NO",
        "ProductGallery": "YES" if args.get("product_gallery", True) else "NO",
        "Network": "YES" if args.get("network", True) else "NO",
    }
    if args.get("dynamic_places") is not None:
        platforms["DynamicPlaces"] = "YES" if args["dynamic_places"] else "NO"

    pkg = {"Platforms": platforms}
    if sid:
        pkg["StrategyId"] = int(sid)
    else:
        pkg["StrategyFromCampaignId"] = int(from_cid)

    # Директ запрещает слать BiddingStrategy/PriorityGoals/CounterIds/AttributionModel вместе с пакетом
    item = {"Id": cid, subtype: {"PackageBiddingStrategy": pkg}}
    data = await _api_write(client, c, "campaigns", "update", {"Campaigns": [item]})
    after = await _campaigns_money_raw(client, c, ids=[cid])
    return _result({
        "Result": data.get("result", data),
        "After": _summarize(_flatten(after[0])) if after else None,
    })


async def _handle_package_strategy_unlink(client, args, config):
    """Отвязка от пакета. Директ требует одновременно задать собственную стратегию."""
    c = _cfg(config)
    cid = int(args["campaign_id"])
    camps = await _campaigns_money_raw(client, c, ids=[cid])
    if not camps:
        raise ValueError("Кампания %s не найдена через campaigns.get." % cid)
    subtype = _flatten(camps[0]).get("CampaignSubtype") or "TextCampaign"

    item = {"Id": cid, subtype: {
        "PackageBiddingStrategy": None,
        "BiddingStrategy": {
            "Search": _build_side(args["search_type"], args, None),
            "Network": _build_side(args["network_type"], args, None),
        },
    }}
    data = await _api_write(client, c, "campaigns", "update", {"Campaigns": [item]})
    after = await _campaigns_money_raw(client, c, ids=[cid])
    return _result({
        "Result": data.get("result", data),
        "After": _summarize(_flatten(after[0])) if after else None,
    })


async def _handle_strategies_action(client, args, config):
    c = _cfg(config)
    action = (args.get("action") or "archive").lower()
    if action not in ("archive", "unarchive"):
        raise ValueError("action должен быть archive или unarchive.")
    data = await _api(client, "strategies", action,
                      {"SelectionCriteria": {"Ids": [int(args["strategy_id"])]}}, **c)
    return _result({"Action": action, "Result": data.get("result", data)})




# ── ручные ставки и аукцион ────────────────────────────────────────────
# У Директа больше нет «позиций» (спецразмещение/гарантия) — современный аукцион
# описывается ОБЪЁМОМ ТРАФИКА: сколько процентов возможных показов заберёт фраза.
# AuctionBids — лестница «объём трафика -> ставка -> списываемая цена». Значения
# объёма бывают и больше 100 (верхние блоки), поэтому потолок сверху не режем.

KEYWORDBID_FIELDS = ["KeywordId", "AdGroupId", "CampaignId", "ServingStatus"]
KEYWORDBID_BLOCKS = [
    {"fn": "SearchFieldNames", "fields": ["Bid", "AuctionBids", "AutotargetingSearchBidIsAuto"]},
    {"fn": "NetworkFieldNames", "fields": ["Bid", "Coverage"]},
]
DEFAULT_TRAFFIC_POINTS = [5, 15, 30, 50, 75, 100]


def _selection_for_bids(args):
    sel = {}
    if args.get("campaign_ids"):
        sel["CampaignIds"] = [int(x) for x in args["campaign_ids"]][:10]
    if args.get("ad_group_ids"):
        sel["AdGroupIds"] = [int(x) for x in args["ad_group_ids"]][:1000]
    if args.get("keyword_ids"):
        sel["KeywordIds"] = [int(x) for x in args["keyword_ids"]][:10000]
    if not sel:
        raise ValueError("Нужен хотя бы один из campaign_ids (до 10), ad_group_ids (до 1000), keyword_ids (до 10000).")
    return sel


def _ladder(search_block):
    """Разворачивает AuctionBids и сортирует по объёму трафика.

    Обёртка у Директа не единообразна: здесь это AuctionBidItems, а не привычный Items,
    поэтому берём любой список внутри.
    """
    ab = (search_block or {}).get("AuctionBids")
    if isinstance(ab, dict):
        ab = (ab.get("AuctionBidItems") or ab.get("Items")
              or next((v for v in ab.values() if isinstance(v, list)), []))
    items = [x for x in (ab or []) if isinstance(x, dict) and x.get("TrafficVolume") is not None]
    return sorted(items, key=lambda x: x["TrafficVolume"])


def _volume_at_bid(ladder, bid):
    """Какой объём трафика фраза берёт при текущей ставке."""
    if not ladder or bid is None:
        return None
    got = [x["TrafficVolume"] for x in ladder if x["Bid"] <= bid]
    return max(got) if got else None


def _bid_for_volume(ladder, target):
    """Ставка, нужная для целевого объёма трафика (ближайшая ступень не ниже цели)."""
    for x in ladder:
        if x["TrafficVolume"] >= target:
            return x["Bid"]
    return ladder[-1]["Bid"] if ladder else None


def _median(xs):
    xs = sorted(v for v in xs if v is not None)
    if not xs:
        return None
    n = len(xs)
    return xs[n // 2] if n % 2 else round((xs[n // 2 - 1] + xs[n // 2]) / 2, 2)


async def _keyword_bids_raw(client, c, args):
    return await _get_degrading(
        client, c, "keywordbids", KEYWORDBID_BLOCKS, "keywordbids",
        {"FieldNames": list(KEYWORDBID_FIELDS), "SelectionCriteria": _selection_for_bids(args)},
        "KeywordBids")


async def _handle_bids_auction_get(client, args, config):
    """Аукцион по фразам: текущая ставка, какой объём трафика она даёт, и чего стоят другие объёмы."""
    c = _cfg(config)
    items = await _keyword_bids_raw(client, c, args)
    points = args.get("traffic_points") or DEFAULT_TRAFFIC_POINTS
    limit = int(args.get("limit") or 25)

    rows, vols, bids = [], [], []
    need = {p: [] for p in points}
    for k in items:
        s = k.get("Search") or {}
        lad = _ladder(s)
        bid = s.get("Bid")
        vol = _volume_at_bid(lad, bid)
        vols.append(vol)
        bids.append(_rub(bid) if bid is not None else None)
        row = {
            "KeywordId": k.get("KeywordId"),
            "AdGroupId": k.get("AdGroupId"),
            "CampaignId": k.get("CampaignId"),
            "ServingStatus": k.get("ServingStatus"),
            "BidRub": _rub(bid) if bid is not None else None,
            "TrafficVolumeNow": vol,
            "NetworkBidRub": _rub((k.get("Network") or {}).get("Bid")) if (k.get("Network") or {}).get("Bid") is not None else None,
        }
        ladder_view = {}
        for p in points:
            b = _bid_for_volume(lad, p)
            if b is not None:
                ladder_view[str(p)] = _rub(b)
                need[p].append(_rub(b))
        row["BidForTraffic"] = ladder_view
        rows.append(row)

    summary = {
        "Keywords": len(items),
        "MedianBidRub": _median(bids),
        "MedianTrafficVolumeNow": _median(vols),
        "MedianBidForTraffic": {str(p): _median(v) for p, v in need.items() if v},
        "NoAuctionData": sum(1 for v in vols if v is None),
    }
    out = {"Summary": summary, "Keywords": rows[:limit]}
    if len(rows) > limit:
        out["Note"] = "Показаны первые %d из %d фраз; сводка посчитана по всем." % (limit, len(rows))
    return _result(out)


async def _handle_bids_set_by_traffic(client, args, config):
    """Автоставки: держать целевой объём трафика на поиске (и/или охват в сетях)."""
    c = _cfg(config)
    sel = _selection_for_bids(args)
    ids = []
    for key, field in (("KeywordIds", "KeywordId"), ("AdGroupIds", "AdGroupId"), ("CampaignIds", "CampaignId")):
        for i in sel.get(key, []):
            ids.append((field, i))

    target = args.get("target_traffic_volume")
    coverage = args.get("target_coverage")
    if target is None and coverage is None:
        raise ValueError("Задай target_traffic_volume (5..100) для поиска и/или target_coverage (1..100) для сетей.")

    def rule(kind, value):
        r = {"TargetTrafficVolume" if kind == "search" else "TargetCoverage": int(value)}
        if args.get("increase_percent") is not None:
            r["IncreasePercent"] = int(args["increase_percent"])
        if args.get("bid_ceiling") is not None:
            r["BidCeiling"] = _mic(args["bid_ceiling"])
        return {"SearchByTrafficVolume": r} if kind == "search" else {"NetworkByCoverage": r}

    # BiddingRule принимает ТОЛЬКО одно из двух правил, поэтому поиск и сети — разными элементами
    payload = []
    for field, i in ids:
        if target is not None:
            payload.append({field: i, "BiddingRule": rule("search", target)})
        if coverage is not None:
            payload.append({field: i, "BiddingRule": rule("network", coverage)})

    data = await _api_write(client, c, "keywordbids", "setAuto", {"KeywordBids": payload})
    res = data.get("result", data)
    errs = [r for r in (res.get("SetAutoResults") or []) if r.get("Errors")]
    return _result({
        "Applied": len(payload),
        "TargetTrafficVolume": target,
        "TargetCoverage": coverage,
        "BidCeilingRub": args.get("bid_ceiling"),
        "Errors": errs[:10],
        "Result": res if not errs else None,
    })


async def _handle_bids_manual_set(client, args, config):
    """Ручные ставки. Либо явная ставка, либо подогнать под нужный объём трафика по аукциону."""
    c = _cfg(config)
    sel = _selection_for_bids(args)
    ceiling = _mic(args.get("bid_ceiling"))
    from_vol = args.get("from_traffic_volume")

    if from_vol is not None:
        # снимаем аукцион и ставим каждой фразе ставку её собственной ступени
        items = await _keyword_bids_raw(client, c, args)
        only_lower = bool(args.get("only_lower"))
        payload, applied, before = [], [], []
        skipped_no_auction = skipped_not_lower = 0
        for k in items:
            cur = (k.get("Search") or {}).get("Bid")
            lad = _ladder(k.get("Search") or {})
            b = _bid_for_volume(lad, int(from_vol))
            if b is None:
                skipped_no_auction += 1
                continue
            if ceiling is not None:
                b = min(b, ceiling)
            if only_lower and cur is not None and b >= cur:
                skipped_not_lower += 1
                continue
            payload.append({"KeywordId": k["KeywordId"], "SearchBid": int(b)})
            applied.append(_rub(b))
            before.append(_rub(cur) if cur is not None else None)
        if not payload:
            raise ValueError(
                "Менять нечего: без данных аукциона %d фраз, уже не выше цели %d."
                % (skipped_no_auction, skipped_not_lower))

        summary = {
            "Mode": "по аукциону" + (", только вниз" if only_lower else ""),
            "FromTrafficVolume": from_vol,
            "KeywordsChanged": len(payload),
            "SkippedNoAuction": skipped_no_auction,
            "SkippedNotLower": skipped_not_lower,
            "BidRubBefore": {"median": _median(before), "sum": round(sum(x for x in before if x), 2)},
            "BidRubAfter": {"min": min(applied), "median": _median(applied), "max": max(applied),
                            "sum": round(sum(applied), 2)},
            "BidCeilingRub": args.get("bid_ceiling"),
        }
        if args.get("preview"):
            summary["Preview"] = True
            summary["Sample"] = [
                {"KeywordId": p["KeywordId"], "BidRubBefore": before[i], "BidRubAfter": applied[i]}
                for i, p in enumerate(payload[:10])
            ]
            return _result(summary)

        data = await _api_write(client, c, "keywordbids", "set", {"KeywordBids": payload})
        res = data.get("result", data)
        summary["Errors"] = [r for r in (res.get("SetResults") or []) if r.get("Errors")][:10]
        return _result(summary)

    if args.get("search_bid") is None and args.get("network_bid") is None:
        raise ValueError("Задай search_bid и/или network_bid в рублях, либо from_traffic_volume.")

    payload = []
    for key, field in (("KeywordIds", "KeywordId"), ("AdGroupIds", "AdGroupId"), ("CampaignIds", "CampaignId")):
        for i in sel.get(key, []):
            item = {field: i}
            if args.get("search_bid") is not None:
                item["SearchBid"] = _mic(args["search_bid"])
            if args.get("network_bid") is not None:
                item["NetworkBid"] = _mic(args["network_bid"])
            payload.append(item)
    data = await _api_write(client, c, "keywordbids", "set", {"KeywordBids": payload})
    res = data.get("result", data)
    errs = [r for r in (res.get("SetResults") or []) if r.get("Errors")]
    return _result({
        "Mode": "явная ставка",
        "Applied": len(payload),
        "SearchBidRub": args.get("search_bid"),
        "NetworkBidRub": args.get("network_bid"),
        "Errors": errs[:10],
    })


async def _handle_strategies_add(client, args, config):
    """Создать пакетную стратегию."""
    c = _cfg(config)
    kind = args["strategy_kind"]
    if kind == "HighestPosition":
        raise ValueError("HighestPosition пакетной стратегией не создаётся — это ручные ставки кампании.")
    item = {"Name": args["name"], kind: _build_block(kind, args, None)}
    pg = _build_priority_goals(args, None)
    if pg:
        item["PriorityGoals"] = pg
    if args.get("counter_ids"):
        item["CounterIds"] = {"Items": [int(x) for x in args["counter_ids"]]}
    if args.get("attribution_model"):
        item["AttributionModel"] = args["attribution_model"]
    data = await _api_write(client, c, "strategies", "add", {"Strategies": [item]})
    res = data.get("result", data)
    new_ids = [r.get("Id") for r in (res.get("AddResults") or []) if r.get("Id")]
    after = await _strategies_get_raw(client, c, ids=new_ids) if new_ids else []
    return _result({"Result": res, "Created": [_humanize(s) for s in after]})


# ── описания тулов ─────────────────────────────────────────────────────

_BID_TARGETS = {"campaign_ids": {"type": "array", "items": {"type": "integer"}, "description": "ID кампаний (до 10)"}, "ad_group_ids": {"type": "array", "items": {"type": "integer"}, "description": "ID групп (до 1000)"}, "keyword_ids": {"type": "array", "items": {"type": "integer"}, "description": "ID фраз (до 10000)"}}

_MONEY_ARGS = {
    "cpa": {"type": "number", "description": "Цена конверсии / целевой CPA, рубли"},
    "average_cpc": {"type": "number", "description": "Средняя цена клика, рубли"},
    "crr": {"type": "integer", "description": "Доля рекламных расходов (CRR), %"},
    "weekly_spend_limit": {"type": "number", "description": "Недельный бюджет, рубли"},
    "bid_ceiling": {"type": "number", "description": "Максимальная ставка (потолок), рубли"},
    "goal_id": {"type": "integer", "description": "ID цели Метрики"},
}

MONEY_TOOLS = [
    Tool(
        name="yd_money_overview",
        description=("Полная картина по деньгам кампаний: тип стратегии на поиске и в сетях с цифрами "
                     "(CPA/CRR/цена клика/недельный бюджет/потолок ставки/цель), дневной бюджет и "
                     "принадлежность к ПАКЕТНОЙ стратегии с её настройками. Суммы в рублях. "
                     "Штатный campaigns_get эти поля не запрашивает и стратегию не показывает."),
        inputSchema={
            "type": "object",
            "properties": {
                "ids": {"type": "array", "items": {"type": "integer"}, "description": "ID кампаний"},
                "states": {"type": "array", "items": {"type": "string"},
                           "description": "По умолчанию ON и SUSPENDED"},
                "raw": {"type": "boolean", "description": "Добавить сырые BiddingStrategy и Funds"},
            },
        },
    ),
    Tool(
        name="yd_strategies_get",
        description=("Прочитать ПАКЕТНЫЕ стратегии аккаунта: тип, недельный бюджет, CPA/CRR, потолок "
                     "ставки, цель, модель атрибуции, счётчики, архивность. Суммы в рублях."),
        inputSchema={
            "type": "object",
            "properties": {
                "ids": {"type": "array", "items": {"type": "integer"}, "description": "ID стратегий"},
                "types": {"type": "array", "items": {"type": "string"}, "description": "Фильтр по типам"},
                "archived": {"type": "boolean", "description": "true — только архивные, false — только живые. По умолчанию живые."},
            },
        },
    ),
    Tool(
        name="yd_account_money",
        description="Деньги аккаунта: валюта, овердрафт, режим оплаты (общий счёт / по кампаниям), НДС, ограничения.",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="yd_strategies_update",
        description=("Изменить ПАКЕТНУЮ стратегию: CPA, CRR, среднюю цену клика, недельный бюджет, "
                     "потолок ставки, цель, имя, а также СТАВКИ ЗА КОНВЕРСИЮ по целям (priority_goals). "
                     "Применяется мгновенно ко ВСЕМ кампаниям пакета. "
                     "Единственный способ поменять деньги кампании из пакета (иначе 5005). "
                     "Незаданные параметры наследуются из текущих настроек. Суммы в рублях."),
        inputSchema={
            "type": "object",
            "properties": dict(_MONEY_ARGS, **{
                "strategy_id": {"type": "integer", "description": "ID пакетной стратегии"},
                "name": {"type": "string", "description": "Новое имя стратегии"},
                "strategy_kind": {"type": "string", "enum": sorted(STRATEGY_BLOCKS),
                                  "description": "Задать тип явно, если автоопределение не сработало"},
                "priority_goals": {
                    "type": "array",
                    "description": ("Ставки за конверсию по целям (для стратегий за конверсию и для "
                                    "ценностей целей). Перечисленные цели меняются, остальные остаются."),
                    "items": {
                        "type": "object",
                        "properties": {
                            "goal_id": {"type": "integer", "description": "ID цели Метрики"},
                            "value": {"type": "number", "description": "Ставка за конверсию, рубли"},
                        },
                        "required": ["goal_id", "value"],
                    },
                },
                "goal_value": {"type": "number",
                               "description": "Одна ставка за конверсию на ВСЕ текущие цели, рубли"},
            }),
            "required": ["strategy_id"],
        },
    ),
    Tool(
        name="yd_strategy_set",
        description=("Задать стратегию ОДНОЙ кампании (не пакетной): тип на поиске и в сетях, CPA/CRR/"
                     "цена клика, недельный бюджет, потолок ставки, цель, доля бюджета в сетях. "
                     "Незаданные параметры НАСЛЕДУЮТСЯ из текущей стратегии, а не обнуляются. "
                     "Сам определяет тип кампании (Text/Unified/...). Если кампания в пакете — вернёт "
                     "понятную ошибку с ID пакета вместо голого 5005. Суммы в рублях."),
        inputSchema={
            "type": "object",
            "properties": dict(_MONEY_ARGS, **{
                "campaign_id": {"type": "integer", "description": "ID кампании"},
                "search_type": {"type": "string", "enum": SEARCH_TYPES, "description": "Стратегия на поиске"},
                "network_type": {"type": "string", "enum": NETWORK_TYPES, "description": "Стратегия в сетях"},
                "network_limit_percent": {"type": "integer",
                                          "description": "Доля бюджета в сетях для NETWORK_DEFAULT, % (10..100)"},
                "strategy_search": {"type": "string", "description": "Алиас search_type (старое имя)"},
                "strategy_network": {"type": "string", "description": "Алиас network_type (старое имя)"},
            }),
            "required": ["campaign_id"],
        },
    ),
    Tool(
        name="yd_daily_budget_set",
        description=("Дневной бюджет кампании: сумма в рублях и режим STANDARD (равномерно) / "
                     "DISTRIBUTED (ускоренно). amount=0 снимает дневной бюджет."),
        inputSchema={
            "type": "object",
            "properties": {
                "campaign_id": {"type": "integer", "description": "ID кампании"},
                "amount": {"type": "number", "description": "Дневной бюджет, рубли (0 — снять)"},
                "mode": {"type": "string", "enum": ["STANDARD", "DISTRIBUTED"], "description": "Режим показов"},
            },
            "required": ["campaign_id", "amount"],
        },
    ),
    Tool(
        name="yd_package_strategy_link",
        description=("Привязать кампанию к пакетной стратегии (strategy_id) либо создать пакет из "
                     "стратегии другой кампании (strategy_from_campaign_id). Площадки задаются флагами."),
        inputSchema={
            "type": "object",
            "properties": {
                "campaign_id": {"type": "integer", "description": "ID кампании"},
                "strategy_id": {"type": "integer", "description": "ID существующей пакетной стратегии"},
                "strategy_from_campaign_id": {"type": "integer",
                                              "description": "ID кампании, из стратегии которой создать пакет"},
                "search_result": {"type": "boolean", "description": "Поисковая выдача (по умолчанию да)"},
                "product_gallery": {"type": "boolean", "description": "Товарная галерея (по умолчанию да)"},
                "network": {"type": "boolean", "description": "РСЯ (по умолчанию да)"},
                "dynamic_places": {"type": "boolean", "description": "Динамические места на поиске"},
            },
            "required": ["campaign_id"],
        },
    ),
    Tool(
        name="yd_package_strategy_unlink",
        description=("Отвязать кампанию от пакетной стратегии. Директ требует одновременно задать "
                     "собственную стратегию, поэтому search_type и network_type обязательны."),
        inputSchema={
            "type": "object",
            "properties": dict(_MONEY_ARGS, **{
                "campaign_id": {"type": "integer", "description": "ID кампании"},
                "search_type": {"type": "string", "enum": SEARCH_TYPES, "description": "Новая стратегия на поиске"},
                "network_type": {"type": "string", "enum": NETWORK_TYPES, "description": "Новая стратегия в сетях"},
                "network_limit_percent": {"type": "integer", "description": "Доля бюджета в сетях, %"},
            }),
            "required": ["campaign_id", "search_type", "network_type"],
        },
    ),
    Tool(
        name="yd_strategies_action",
        description="Архивировать или разархивировать пакетную стратегию.",
        inputSchema={
            "type": "object",
            "properties": {
                "strategy_id": {"type": "integer", "description": "ID пакетной стратегии"},
                "action": {"type": "string", "enum": ["archive", "unarchive"], "description": "По умолчанию archive"},
            },
            "required": ["strategy_id"],
        },
    ),
    Tool(
        name="yd_bids_auction_get",
        description=("Аукцион по фразам: текущая ставка, КАКОЙ ОБЪЁМ ТРАФИКА она сейчас даёт, и сколько "
                     "стоит каждый объём (лестница «объём трафика -> ставка -> списываемая цена»). "
                     "У Директа больше нет позиций (спецразмещение/гарантия) — это их замена. "
                     "Суммы в рублях, сводка по всей выборке + разбор по фразам."),
        inputSchema={
            "type": "object",
            "properties": dict(_BID_TARGETS, **{
                "traffic_points": {"type": "array", "items": {"type": "integer"},
                                   "description": "Какие объёмы трафика показать, по умолчанию 5/15/30/50/75/100"},
                "limit": {"type": "integer", "description": "Сколько фраз показать построчно (по умолчанию 25)"},
            }),
        },
    ),
    Tool(
        name="yd_bids_set_by_traffic",
        description=("Автоставки: держать целевой объём трафика на поиске (5..100) и/или охват в сетях (1..100). "
                     "Директ сам пересчитывает ставку под цель. Можно задать надбавку и потолок ставки в рублях. "
                     "Ставится на фразы, группы или кампании целиком."),
        inputSchema={
            "type": "object",
            "properties": dict(_BID_TARGETS, **{
                "target_traffic_volume": {"type": "integer", "description": "Целевой объём трафика на поиске, 5..100"},
                "target_coverage": {"type": "integer", "description": "Целевой охват в сетях, 1..100"},
                "increase_percent": {"type": "integer", "description": "Надбавка к расчётной ставке, 0..1000 %"},
                "bid_ceiling": {"type": "number", "description": "Потолок ставки, рубли"},
            }),
        },
    ),
    Tool(
        name="yd_bids_manual_set",
        description=("Ручные ставки. Либо явная ставка (search_bid / network_bid в рублях) на фразы, группы "
                     "или кампании, либо from_traffic_volume — снять аукцион и выставить каждой фразе её "
                     "собственную ставку под нужный объём трафика, с потолком. only_lower=true только понижает, "
                     "preview=true показывает результат без записи."),
        inputSchema={
            "type": "object",
            "properties": dict(_BID_TARGETS, **{
                "search_bid": {"type": "number", "description": "Ставка на поиске, рубли"},
                "network_bid": {"type": "number", "description": "Ставка в сетях, рубли"},
                "from_traffic_volume": {"type": "integer",
                                        "description": "Подогнать ставку под этот объём трафика по текущему аукциону, 5..100"},
                "bid_ceiling": {"type": "number", "description": "Потолок ставки для режима from_traffic_volume, рубли"},
                "only_lower": {"type": "boolean",
                               "description": "Только понижать: фразы, где расчётная ставка не ниже текущей, не трогать"},
                "preview": {"type": "boolean",
                            "description": "Ничего не писать, показать что изменится (для from_traffic_volume)"},
            }),
        },
    ),
    Tool(
        name="yd_strategies_add",
        description=("Создать ПАКЕТНУЮ стратегию: тип, недельный бюджет, CPA/CRR/цена клика, цель, ставки за "
                     "конверсию по целям, счётчики, модель атрибуции. Суммы в рублях. Кампании привязываются "
                     "потом через yandex_direct_package_strategy_link."),
        inputSchema={
            "type": "object",
            "properties": dict(_MONEY_ARGS, **{
                "name": {"type": "string", "description": "Название пакетной стратегии"},
                "strategy_kind": {"type": "string", "enum": sorted(k for k in STRATEGY_BLOCKS if k != "HighestPosition"),
                                  "description": "Тип стратегии"},
                "counter_ids": {"type": "array", "items": {"type": "integer"}, "description": "Счётчики Метрики"},
                "attribution_model": {"type": "string", "enum": ["AUTO", "FCCD", "LC", "LSCCD"]},
                "priority_goals": {
                    "type": "array",
                    "description": "Ставки за конверсию по целям (обязательно для multiple-goals типов, минимум 2 цели)",
                    "items": {"type": "object", "properties": {
                        "goal_id": {"type": "integer"}, "value": {"type": "number", "description": "рубли"}},
                        "required": ["goal_id", "value"]},
                },
            }),
            "required": ["name", "strategy_kind"],
        },
    ),
]

_MONEY_HANDLERS = {
    "yd_money_overview": _handle_money_overview,
    "yd_strategies_get": _handle_strategies_get,
    "yd_account_money": _handle_account_money,
    "yd_strategies_update": _handle_strategies_update,
    "yd_strategy_set": _handle_strategy_set,
    "yd_daily_budget_set": _handle_daily_budget_set,
    "yd_package_strategy_link": _handle_package_strategy_link,
    "yd_package_strategy_unlink": _handle_package_strategy_unlink,
    "yd_strategies_action": _handle_strategies_action,
    "yd_bids_auction_get": _handle_bids_auction_get,
    "yd_bids_set_by_traffic": _handle_bids_set_by_traffic,
    "yd_bids_manual_set": _handle_bids_manual_set,
    "yd_strategies_add": _handle_strategies_add,
}


def register_money_handlers(dispatch: dict):
    """Добавляет денежные хендлеры в диспетчер форка.

    Заодно перенаправляет старый yd_campaign_strategy_update на безопасный обработчик:
    прежний собирал стратегию только из переданных полей и тем самым сносил недельный
    бюджет при правке одного CPA, а ещё был жёстко привязан к TextCampaign.
    """
    dispatch.update(_MONEY_HANDLERS)
    dispatch["yd_campaign_strategy_update"] = _handle_strategy_set
