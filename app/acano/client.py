"""HTTP client for ACANO API (REST + SOAP fallback)."""
import os
import httpx
from app.config import ACANO_BASE_URL, ACANO_AUTH_TOKEN, ACANO_WSDL_URL, ACANO_PRODUCTS_ENDPOINT


def _get_proxy() -> dict | None:
    """Return httpx proxy config if FIXIE_URL is set."""
    fixie_url = os.getenv("FIXIE_URL")
    if fixie_url:
        return {"http://": fixie_url, "https://": fixie_url}
    return None


class AcanoClient:
    """Fetches product/credit data from ACANO."""

    async def fetch_products(self) -> list[dict]:
        if ACANO_WSDL_URL:
            return self._fetch_soap()
        return await self._fetch_rest()

    async def _fetch_rest(self) -> list[dict]:
        async with httpx.AsyncClient(timeout=120, proxies=_get_proxy()) as client:
            response = await client.get(
                f"{ACANO_BASE_URL}{ACANO_PRODUCTS_ENDPOINT}",
                headers={"Authorization": f"Bearer {ACANO_AUTH_TOKEN}"}
            )
            response.raise_for_status()
            data = response.json()
            # Handle if API wraps records in a key like {"data": [...]} or {"records": [...]}
            if isinstance(data, dict):
                for key in ("data", "records", "productos", "items", "result"):
                    if key in data and isinstance(data[key], list):
                        return data[key]
                raise ValueError(f"ACANO API returned dict but no recognizable list key. Keys: {list(data.keys())}")
            return data

    def _fetch_soap(self) -> list[dict]:
        try:
            from zeep import Client
        except ImportError:
            raise RuntimeError("Install zeep for SOAP support: pip install zeep")
        soap = Client(ACANO_WSDL_URL)
        result = soap.service.ConsultaProductos(token=ACANO_AUTH_TOKEN)
        return [dict(item) for item in result]
