#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
BDO Lifeskill — Atualizador de mercado SA
Versão 3

CORREÇÃO PRINCIPAL:
O endpoint /v2/sa/market NÃO devolve priceMin/priceMax/lastSoldPrice.
Ele devolve apenas informações resumidas do mercado.

O site BDO-Harmonia já usa corretamente:
    POST https://api.arsha.io/v1/sa/item

Esse arquivo reproduz essa lógica no GitHub Actions:
1. Lê os IDs em DATA.priceIds do index.html.
2. Consulta todos os IDs em lote no endpoint V1.
3. Extrai estoque, negociações, preço mínimo, máximo e último preço vendido.
4. Gera mercado.json para o GitHub Pages.
5. Preserva dados anteriores quando algum item não responder.
6. Nunca troca um mercado válido por um arquivo vazio.
"""

from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parent
INDEX_FILE = ROOT / "index.html"
MARKET_FILE = ROOT / "mercado.json"

API_ITEM_URL = "https://api.arsha.io/v1/sa/item"
API_HISTORY_URL = "https://api.arsha.io/v1/sa/history"

USER_AGENT = "BDO-Habilidades-para-a-Vida/3.0"
TIMEOUT = 45
MAX_RETRIES = 4
BATCH_SIZE = 100
SCHEMA_VERSION = 3


# Fallback usado somente se o index.html não possuir DATA.priceIds.
# A lista completa é obtida automaticamente do site sempre que possível.
FALLBACK_PRICE_IDS = {
    "Fármaco da Harmonia": 1399,
    "Fármaco da Harmonia - Edania": 1407,
    "Fármaco da Raiva": 1389,
    "Fármaco da Adaptação": 1391,
    "Fármaco do Potencial": 1393,
    "Fármaco da Decadência": 1395,
    "Fármaco da Ira Descontrolada": 1397,
    "Elixir de Edania": 1409,
    "Elixir de Fúria": 704,
    "Elixir do Frenesi": 672,
    "Elixir da Concentração": 700,
    "Elixir da Destruição": 1180,
    "Elixir da Defesa": 716,
    "Elixir de Espiral": 782,
    "Elixir de Vida": 708,
    "Elixir de Estamina": 722,
    "Elixir de Choque": 762,
    "Elixir de Feitiço": 692,
    "Elixir de Rapidez": 690,
    "Elixir do Vento": 688,
    "Elixir de Perfuração": 617,
    "Elixir da Morte": 686,
    "Elixir de Pilhagem": 676,
    "Elixir do Ceifador": 649,
    "Elixir de Assassinato": 634,
    "Elixir de Carnificina": 660,
    "Elixir de Detecção": 636,
    "Elixir do Céu": 720,
    "Elixir da Vontade": 702,
    "Água Purificada": 1435,
    "Reagente Líquido Limpo": 5301,
    "Reagente em pó Puro": 5302,
    "Grama Selvagem": 5439,
    "Cogumelo Anão": 5409,
    "Cogumelo Fantasma": 5414,
    "Seiva de Freixo": 5001,
    "Seiva de Bétula": 5004,
    "Seiva de Cedro": 5010,
    "Sangue de Porco": 6205,
    "Sangue de Ovelha": 6202,
    "Sangue de Cervo": 6201,
    "Sangue de Lobo": 6214,
    "Sangue de Urso": 6213,
    "Sangue de Palhaço": 6353,
    "Sangue da Fera Lendária": 6351,
    "Catalisador Mágico": 820936,
    "Pó de Chama": 4802,
    "Vestígio da Natureza": 60458,
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def request_json(
    url: str,
    *,
    method: str = "GET",
    body: Any = None,
) -> Any:
    last_error: Exception | None = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            headers = {
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
                "Cache-Control": "no-cache",
            }

            data = None
            if body is not None:
                data = json.dumps(body, separators=(",", ":")).encode("utf-8")
                headers["Content-Type"] = "application/json"

            req = Request(
                url,
                data=data,
                headers=headers,
                method=method,
            )

            with urlopen(req, timeout=TIMEOUT) as response:
                raw = response.read()

            if not raw:
                raise RuntimeError("A API respondeu sem conteúdo.")

            return json.loads(raw.decode("utf-8"))

        except (
            HTTPError,
            URLError,
            TimeoutError,
            OSError,
            json.JSONDecodeError,
            RuntimeError,
        ) as exc:
            last_error = exc
            print(
                f"[AVISO] {method} {url} — tentativa "
                f"{attempt}/{MAX_RETRIES}: {exc}",
                flush=True,
            )

            if attempt < MAX_RETRIES:
                time.sleep(2 * attempt)

    raise RuntimeError(
        f"Falha definitiva na consulta {url}: {last_error}"
    )


def extract_price_ids_from_index() -> dict[str, int]:
    """
    Extrai DATA.priceIds diretamente do index.html.

    Isso é importante porque o site pode crescer de 50 para 153+ itens.
    O updater não precisa ser alterado toda vez que novos materiais
    forem adicionados ao site.
    """
    if not INDEX_FILE.exists():
        print(
            "[AVISO] index.html não encontrado; usando lista de fallback.",
            flush=True,
        )
        return dict(FALLBACK_PRICE_IDS)

    html = INDEX_FILE.read_text(encoding="utf-8", errors="replace")

    # Procura o objeto:
    # "priceIds":{ ... } ,"verifiedApi"
    match = re.search(
        r'"priceIds"\s*:\s*(\{.*?\})\s*,\s*"verifiedApi"',
        html,
        flags=re.DOTALL,
    )

    if not match:
        # Algumas versões podem usar priceIds sem verifiedApi logo depois.
        match = re.search(
            r'"priceIds"\s*:\s*(\{.*?\})\s*(?:[,}])',
            html,
            flags=re.DOTALL,
        )

    if match:
        raw = match.group(1)

        try:
            obj = json.loads(raw)
            parsed = {
                str(name): int(item_id)
                for name, item_id in obj.items()
                if str(item_id).isdigit()
            }

            if parsed:
                print(
                    f"[OK] DATA.priceIds encontrado no index.html: "
                    f"{len(parsed)} itens.",
                    flush=True,
                )
                return parsed

        except (json.JSONDecodeError, ValueError) as exc:
            print(
                f"[AVISO] não foi possível interpretar DATA.priceIds: {exc}",
                flush=True,
            )

    print(
        "[AVISO] DATA.priceIds não foi localizado; "
        "usando lista de fallback.",
        flush=True,
    )
    return dict(FALLBACK_PRICE_IDS)


def empty_market() -> dict[str, Any]:
    return {
        "versão": SCHEMA_VERSION,
        "Fonte": "Arsha.io BDO Market API — SA",
        "região": "SA",
        "geradoEm": None,
        "totalItens": 0,
        "recebidos": 0,
        "esperados": 0,
        "Unid": [],
        "história": {},
        "histórico de vendas": {},
        "Instantâneo de vendas": {},
    }


def load_existing() -> dict[str, Any]:
    if not MARKET_FILE.exists():
        return empty_market()

    try:
        data = json.loads(MARKET_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception as exc:
        print(
            f"[AVISO] mercado.json existente não pôde ser lido: {exc}",
            flush=True,
        )

    return empty_market()


def parse_v1_response(payload: Any) -> dict[int, dict[str, Any]]:
    """
    V1 /item retorna resultMsg no formato:

    id-sid-minEnhance-maxEnhance-basePrice-stock-totalTrades-
    priceMin-priceMax-lastSoldPrice-lastSoldTime

    Para cada item usamos a linha base sid=0.
    """
    by_id: dict[int, dict[str, Any]] = {}

    objects = payload if isinstance(payload, list) else [payload]

    for obj in objects:
        if not isinstance(obj, dict):
            continue

        result_msg = obj.get("resultMsg")
        if not isinstance(result_msg, str):
            continue

        for part in result_msg.split("|"):
            if not part:
                continue

            fields = part.split("-")

            if len(fields) < 10:
                continue

            try:
                item_id = int(fields[0])
                sid = int(fields[1])
                min_enhance = int(fields[2])
                max_enhance = int(fields[3])

                if sid != 0 or min_enhance != 0 or max_enhance != 0:
                    continue

                by_id[item_id] = {
                    "id": item_id,
                    "sid": sid,
                    "minEnhance": min_enhance,
                    "maxEnhance": max_enhance,
                    "basePrice": int(fields[4]),
                    "currentStock": int(fields[5]),
                    "totalTrades": int(fields[6]),
                    "priceMin": int(fields[7]),
                    "priceMax": int(fields[8]),
                    "lastSoldPrice": int(fields[9]),
                    "lastSoldTime": int(fields[10])
                    if len(fields) > 10
                    else 0,
                }

            except (ValueError, IndexError):
                continue

    return by_id


def fetch_items(price_ids: dict[str, int]) -> dict[int, dict[str, Any]]:
    ids = sorted(set(price_ids.values()))

    print(
        f"[INFO] consultando {len(ids)} IDs do site via "
        f"POST /v1/sa/item...",
        flush=True,
    )

    result: dict[int, dict[str, Any]] = {}

    for start in range(0, len(ids), BATCH_SIZE):
        batch = ids[start : start + BATCH_SIZE]

        print(
            f"[INFO] lote {start + 1}-{start + len(batch)} "
            f"de {len(ids)}...",
            flush=True,
        )

        payload = request_json(
            API_ITEM_URL,
            method="POST",
            body=batch,
        )

        parsed = parse_v1_response(payload)
        result.update(parsed)

        print(
            f"[INFO] lote retornou {len(parsed)} itens válidos.",
            flush=True,
        )

    return result


def previous_by_id(previous: dict[str, Any]) -> dict[int, dict[str, Any]]:
    result: dict[int, dict[str, Any]] = {}

    old_items = previous.get("Unid", [])

    if not isinstance(old_items, list):
        return result

    for item in old_items:
        if not isinstance(item, dict):
            continue

        try:
            item_id = int(item["id"])
        except (KeyError, TypeError, ValueError):
            continue

        result[item_id] = item

    return result


def make_site_items(
    price_ids: dict[str, int],
    api_items: dict[int, dict[str, Any]],
    previous: dict[str, Any],
) -> tuple[list[dict[str, Any]], int, int]:
    old = previous_by_id(previous)

    output: list[dict[str, Any]] = []
    received = 0
    missing = 0

    # Mantém exatamente a lista de itens do site, na mesma ordem.
    for name, item_id in price_ids.items():
        data = api_items.get(item_id)

        if data is None:
            # Se a API não respondeu para este item, preserva o último
            # dado válido em vez de zerar o preço.
            old_item = old.get(item_id)

            if old_item:
                preserved = dict(old_item)
                preserved["name"] = name
                preserved["fonte"] = "cache anterior"
                output.append(preserved)
            else:
                output.append(
                    {
                        "name": name,
                        "id": item_id,
                        "sid": 0,
                        "minEnhance": 0,
                        "maxEnhance": 0,
                        "basePrice": 0,
                        "currentStock": 0,
                        "totalTrades": 0,
                        "priceMin": 0,
                        "priceMax": 0,
                        "lastSoldPrice": 0,
                        "lastSoldTime": 0,
                        "fonte": "sem resposta",
                    }
                )

            missing += 1
            continue

        row = dict(data)
        row["name"] = name
        row["fonte"] = "Arsha.io V1 / SA"
        output.append(row)
        received += 1

    return output, received, missing


def update_history(
    previous: dict[str, Any],
    items: list[dict[str, Any]],
    generated_at: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    history = previous.get("história", {})
    if not isinstance(history, dict):
        history = {}

    sales_history = previous.get("histórico de vendas", {})
    if not isinstance(sales_history, dict):
        sales_history = {}

    snapshots_obj = previous.get("Instantâneo de vendas", {})
    if not isinstance(snapshots_obj, dict):
        snapshots_obj = {}

    snapshots = snapshots_obj.get("snapshots", [])
    if not isinstance(snapshots, list):
        snapshots = []

    snapshots = snapshots[-167:]

    snapshots.append(
        {
            "data": generated_at,
            "itens": len(items),
            "comResposta": sum(
                1
                for item in items
                if item.get("fonte") == "Arsha.io V1 / SA"
            ),
        }
    )

    for item in items:
        item_id = str(item.get("id"))
        key = item_id

        price = int(item.get("priceMin") or 0)
        last = int(item.get("lastSoldPrice") or 0)
        stock = int(item.get("currentStock") or 0)

        values = history.get(key, [])
        if not isinstance(values, list):
            values = []

        values = values[-167:]
        values.append(
            {
                "data": generated_at,
                "preço": price or last,
                "estoque": stock,
                "totalTrades": int(item.get("totalTrades") or 0),
            }
        )
        history[key] = values

        sales_values = sales_history.get(key, [])
        if not isinstance(sales_values, list):
            sales_values = []

        sales_values = sales_values[-167:]
        sales_values.append(
            {
                "data": generated_at,
                "últimoPreço": last,
                "últimaVenda": int(item.get("lastSoldTime") or 0),
                "totalTrades": int(item.get("totalTrades") or 0),
            }
        )
        sales_history[key] = sales_values

    return history, sales_history, {"snapshots": snapshots}


def write_market(
    previous: dict[str, Any],
    items: list[dict[str, Any]],
    expected: int,
    received: int,
    generated_at: str,
) -> None:
    history, sales_history, snapshots = update_history(
        previous,
        items,
        generated_at,
    )

    output = {
        "versão": SCHEMA_VERSION,
        "Fonte": "Arsha.io BDO Market API — SA",
        "região": "SA",
        "geradoEm": generated_at,
        "totalItens": len(items),
        "esperados": expected,
        "recebidos": received,
        "Unid": items,
        "história": history,
        "histórico de vendas": sales_history,
        "Instantâneo de vendas": snapshots,
    }

    tmp = MARKET_FILE.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(output, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    tmp.replace(MARKET_FILE)


def main() -> int:
    print("=" * 70)
    print("BDO — Habilidades para a Vida | Atualizador Mercado SA V3")
    print("=" * 70)

    price_ids = extract_price_ids_from_index()

    if not price_ids:
        print("[ERRO] Nenhum item configurado no site.", flush=True)
        return 1

    previous = load_existing()

    expected = len(price_ids)

    print(f"[INFO] itens esperados pelo site: {expected}", flush=True)
    print(f"[INFO] endpoint correto: {API_ITEM_URL}", flush=True)

    try:
        api_items = fetch_items(price_ids)
    except Exception as exc:
        print(
            f"[ERRO] API não respondeu corretamente: {exc}",
            flush=True,
        )

        old_items = previous.get("Unid", [])
        old_count = len(old_items) if isinstance(old_items, list) else 0

        if old_count:
            print(
                f"[OK] mantendo mercado anterior ({old_count} itens).",
                flush=True,
            )
            return 0

        print(
            "[ERRO] não existe mercado anterior para preservar.",
            flush=True,
        )
        return 1

    items, received, missing = make_site_items(
        price_ids,
        api_items,
        previous,
    )

    print(
        f"[RESULTADO] {received}/{expected} itens receberam resposta.",
        flush=True,
    )
    print(
        f"[RESULTADO] {missing} itens ficaram sem resposta/cache.",
        flush=True,
    )

    # Se a API respondeu a absolutamente nenhum item, não publicamos
    # um arquivo zerado.
    if received == 0:
        print(
            "[ERRO] 0 itens recebidos. mercado.json NÃO será substituído.",
            flush=True,
        )
        return 1

    generated_at = now_iso()

    write_market(
        previous=previous,
        items=items,
        expected=expected,
        received=received,
        generated_at=generated_at,
    )

    print(f"[OK] mercado.json gravado em {generated_at}", flush=True)
    print(f"[OK] itens no arquivo: {len(items)}", flush=True)

    # Diagnóstico dos primeiros itens.
    for item in items[:8]:
        print(
            "[ITEM]",
            item.get("name"),
            "| ID",
            item.get("id"),
            "| estoque",
            item.get("currentStock", 0),
            "| min",
            item.get("priceMin", 0),
            "| último",
            item.get("lastSoldPrice", 0),
            "|",
            item.get("fonte"),
            flush=True,
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
