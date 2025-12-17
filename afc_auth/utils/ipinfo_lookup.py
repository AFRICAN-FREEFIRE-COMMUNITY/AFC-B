import pandas as pd
import pytricia
import ipaddress
import os

IPINFO_CSV_PATH = "/home/ubuntu/ipinfo/ipinfo_lite.csv"

# Create Patricia Trie
pt = pytricia.PyTricia(128)  # supports IPv4 & IPv6


def load_ipinfo():
    if not os.path.exists(IPINFO_CSV_PATH):
        raise FileNotFoundError("IPinfo Lite CSV not found")

    df = pd.read_csv(IPINFO_CSV_PATH)

    for _, row in df.iterrows():
        network = row["network"]
        pt[network] = {
            "country": row.get("country"),
            "country_code": row.get("country_code"),
            "continent": row.get("continent"),
            "continent_code": row.get("continent_code"),
            "asn": row.get("asn"),
            "as_name": row.get("as_name"),
            "as_domain": row.get("as_domain"),
        }


# Load once at startup
load_ipinfo()


def lookup_ip(ip):
    try:
        ip_obj = ipaddress.ip_address(ip)
        return pt.get(str(ip_obj), None)
    except Exception:
        return None
