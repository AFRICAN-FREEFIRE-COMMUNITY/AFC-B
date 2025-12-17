# utils/ipinfo_lookup.py
import pandas as pd
from ipaddress import ip_network, ip_address

IPINFO_FILE = "/home/ubuntu/ipinfo/ipinfo_lite.csv"

print("Loading IP info CSV...")  # will print when server starts
df = pd.read_csv(IPINFO_FILE)
networks = []

for _, row in df.iterrows():
    networks.append({
        "network": ip_network(row["network"]),
        "country": row["country"],
        "country_code": row["country_code"],
        "continent": row.get("continent", None)
    })

print(f"Loaded {len(networks)} IP ranges.")

def lookup_ip(ip):
    try:
        ip_obj = ip_address(ip)
    except ValueError:
        return None

    for net in networks:
        if ip_obj in net["network"]:
            return net
    return None
