# utils/ipinfo_lookup.py

import pandas as pd
import ipaddress
from functools import lru_cache

IPINFO_CSV_PATH = "/home/ubuntu/ipinfo/ipinfo_lite.csv"

_ip_networks = None  # in-memory cache


def load_ipinfo():
    """
    Load IPinfo CSV ONCE into memory.
    """
    global _ip_networks

    if _ip_networks is not None:
        return

    df = pd.read_csv(IPINFO_CSV_PATH)

    networks = []
    for row in df.itertuples(index=False):
        try:
            networks.append({
                "network": ipaddress.ip_network(row.network),
                "country": row.country,
                "country_code": row.country_code,
            })
        except ValueError:
            continue

    _ip_networks = networks


@lru_cache(maxsize=5000)
def lookup_ip(ip_address: str):
    """
    Lookup country info for an IP.
    """
    if not ip_address:
        return None

    load_ipinfo()  # LAZY LOAD

    try:
        ip = ipaddress.ip_address(ip_address)
    except ValueError:
        return None

    for net in _ip_networks:
        if ip in net["network"]:
            return {
                "country": net["country"],
                "country_code": net["country_code"],
            }

    return None
