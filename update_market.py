#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Atualizador do mercado BDO SA para GitHub Actions.

Objetivo:
- Consultar a API comunitária Arsha.io na região SA.
- Gravar um mercado.json estático para o GitHub Pages.
- Manter histórico local de snapshots.
- NUNCA substituir um mercado válido por um arquivo vazio se a API falhar.
- Não depende de servidor Python, proxy CORS ou bibliotecas externas.

Endpoint principal documentado:
https://api.arsha.io/v2/sa/market

O GitHub Actions deve executar este arquivo e depois fazer commit/push
do mercado.json.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parent
MARKET_FILE = ROOT / "mercado.json"

API_URL = "https://api.arsha.io/v2/sa/market"
HOT_URL = "https://api.arsha.io/v2/sa/hot?lang=en"

USER_AGENT = "BDO-Lifeskill-Market/2.0"
TIMEOUT = 45
MAX_RETRIES = 4

# O site atual usa estas chaves. Elas são preservadas para compatibilidade.
SCHEMA_VERSION = 2


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def request_json(url: str) -> Any:
    last_error: Exception | None = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            request = Request(
                url,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "application/json",
                    "Cache-Control": "no-cache",
                },
                method="GET",
            )

            with urlopen(request, timeout=TIMEOUT) as response:
                raw = response.read()
                if not raw:
                    raise RuntimeError("A API respondeu sem conteúdo.")

                return json.loads(raw.decode("utf-8"))

        except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError, RuntimeError) as exc:
            last_error = exc
            print(
                f"[AVISO] tentativa {attempt}/{MAX_RETRIES} falhou: {exc}",
                flush=True,
            )

            if attempt < MAX_RETRIES:
                time.sleep(2 * attempt)

    raise RuntimeError(f"Não foi possível consultar {url}: {last_error}")


def load_existing() -> dict[str, Any]:
    if not MARKET_FILE.exists():
        return {
            "versão": SCHEMA_VERSION,
            "Fonte": "Arsha.io BDO Market SA",
            "geradoEm": None,
            "Unid": [],
            "história": {},
            "histórico de vendas": {},
            "Instantâneo de vendas": {},
        }

    try:
        data = json.loads(MARKET_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception as exc:
        print(f"[AVISO] não foi possível ler mercado.json: {exc}", flush=True)

    return {
        "versão": SCHEMA_VERSION,
        "Fonte": "Arsha.io BDO Market SA",
        "geradoEm": None,
        "Unid": [],
        "história": {},
        "histórico de vendas": {},
        "Instantâneo de vendas": {},
    }


def normalize_item(item: dict[str, Any]) -> dict[str, Any]:
    """
    Normaliza a resposta V2 sem destruir campos que possam ser úteis
    para o site futuramente.
    """
    result = dict(item)

    # IDs e valores numéricos ficam consistentes.
    for key in (
        "id",
        "sid",
        "minEnhance",
        "maxEnhance",
        "currentStock",
        "totalTrades",
        "basePrice",
        "priceMin",
        "priceMax",
        "lastSoldPrice",
        "lastSoldTime",
        "mainCategory",
        "subCategory",
    ):
        if key in result:
            try:
                result[key] = int(result[key])
            except (TypeError, ValueError):
                pass

    # Aliases úteis para versões do site que usam nomes em português.
    if "currentStock" in result:
        result["estoque"] = result["currentStock"]

    if "basePrice" in result:
        result["preçoBase"] = result["basePrice"]

    if "priceMin" in result:
        result["preçoMin"] = result["priceMin"]

    if "priceMax" in result:
        result["preçoMax"] = result["priceMax"]

    if "lastSoldPrice" in result:
        result["últimoPreço"] = result["lastSoldPrice"]

    if "lastSoldTime" in result:
        result["últimaVenda"] = result["lastSoldTime"]

    return result


def valid_market(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, list):
        return []

    items: list[dict[str, Any]] = []

    for item in payload:
        if not isinstance(item, dict):
            continue

        if item.get("id") is None:
            continue

        items.append(normalize_item(item))

    return items


def update_history(
    previous: dict[str, Any],
    items: list[dict[str, Any]],
    generated_at: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    old_history = previous.get("história", {})
    if not isinstance(old_history, dict):
        old_history = {}

    old_sales = previous.get("histórico de vendas", {})
    if not isinstance(old_sales, dict):
        old_sales = {}

    old_snapshots = previous.get("Instantâneo de vendas", {})
    if not isinstance(old_snapshots, dict):
        old_snapshots = {}

    history = dict(old_history)
    sales_history = dict(old_sales)

    # Um snapshot global também é mantido para facilitar gráficos futuros.
    snapshot = {
        "data": generated_at,
        "itens": len(items),
    }

    snapshots = old_snapshots.get("snapshots", [])
    if not isinstance(snapshots, list):
        snapshots = []

    snapshots = snapshots[-167:]  # cerca de 7 dias se rodar ~24 vezes/dia
    snapshots.append(snapshot)

    for item in items:
        item_id = str(item["id"])
        sid = str(item.get("sid", 0))
        key = f"{item_id}:{sid}"

        price = item.get("priceMin")
        if not isinstance(price, (int, float)) or price <= 0:
            price = item.get("lastSoldPrice")

        stock = item.get("currentStock", 0)

        entry = {
            "data": generated_at,
            "preço": price if isinstance(price, (int, float)) else 0,
            "estoque": stock if isinstance(stock, (int, float)) else 0,
            "totalTrades": item.get("totalTrades", 0),
        }

        values = history.get(key, [])
        if not isinstance(values, list):
            values = []

        # Mantém no máximo 7 dias de snapshots a cada execução horária.
        values = values[-167:]
        values.append(entry)
        history[key] = values

        sales_entry = {
            "data": generated_at,
            "últimoPreço": item.get("lastSoldPrice", 0),
            "últimaVenda": item.get("lastSoldTime", 0),
            "totalTrades": item.get("totalTrades", 0),
        }

        sales_values = sales_history.get(key, [])
        if not isinstance(sales_values, list):
            sales_values = []

        sales_values = sales_values[-167:]
        sales_values.append(sales_entry)
        sales_history[key] = sales_values

    return history, sales_history, {"snapshots": snapshots}


def write_market(
    previous: dict[str, Any],
    items: list[dict[str, Any]],
    generated_at: str,
    source: str,
) -> None:
    history, sales_history, snapshots = update_history(
        previous,
        items,
        generated_at,
    )

    output = {
        "versão": SCHEMA_VERSION,
        "Fonte": source,
        "geradoEm": generated_at,
        "região": "SA",
        "totalItens": len(items),

        # Compatibilidade com o formato que o site já utiliza.
        "Unid": items,

        # Histórico por item.
        "história": history,
        "histórico de vendas": sales_history,

        # Snapshots globais.
        "Instantâneo de vendas": snapshots,
    }

    temp_file = MARKET_FILE.with_suffix(".json.tmp")
    temp_file.write_text(
        json.dumps(output, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )

    # Troca atômica: evita deixar mercado.json quebrado se o processo parar.
    temp_file.replace(MARKET_FILE)

    print(
        f"[OK] mercado.json atualizado com {len(items)} itens.",
        flush=True,
    )


def main() -> int:
    print("=" * 60)
    print("BDO Lifeskill — Atualizador de Mercado SA")
    print("=" * 60)

    previous = load_existing()

    print(f"[INFO] arquivo: {MARKET_FILE}")
    print(f"[INFO] API: {API_URL}")

    try:
        payload = request_json(API_URL)
        items = valid_market(payload)

        print(f"[INFO] API retornou {len(items)} itens.")

    except Exception as exc:
        print(f"[ERRO] falha na consulta principal: {exc}", flush=True)
        items = []

    # Segurança: nunca transformar um mercado válido em 0 itens.
    previous_items = previous.get("Unid", [])
    previous_count = len(previous_items) if isinstance(previous_items, list) else 0

    if not items:
        print(
            "[ERRO] A API não retornou itens válidos. "
            "O mercado.json existente NÃO será substituído.",
            flush=True,
        )

        # Tenta o endpoint hot apenas para diagnóstico.
        try:
            hot_payload = request_json(HOT_URL)
            hot_items = valid_market(hot_payload)
            print(
                f"[INFO] fallback /hot retornou {len(hot_items)} itens.",
                flush=True,
            )
        except Exception as exc:
            print(f"[INFO] fallback /hot também falhou: {exc}", flush=True)

        if previous_count:
            print(
                f"[INFO] mantendo o mercado anterior com {previous_count} itens.",
                flush=True,
            )
            return 0

        print(
            "[ERRO] não existe mercado anterior válido para preservar.",
            flush=True,
        )
        return 1

    generated_at = now_iso()

    write_market(
        previous=previous,
        items=items,
        generated_at=generated_at,
        source="Arsha.io BDO Market API — SA",
    )

    # Resumo para o log do GitHub Actions.
    print(f"[OK] geração concluída em {generated_at}")
    print(f"[OK] itens publicados: {len(items)}")

    # Mostra alguns itens para facilitar diagnóstico do Actions.
    for item in items[:5]:
        print(
            "[ITEM]",
            item.get("id"),
            item.get("name", "Sem nome"),
            "stock=",
            item.get("currentStock", 0),
            "priceMin=",
            item.get("priceMin", 0),
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
