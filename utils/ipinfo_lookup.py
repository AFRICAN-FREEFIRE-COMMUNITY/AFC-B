# import geoip2.database

# MMDB_PATH = "/home/ubuntu/ipinfo/ipinfo_lite.mmdb"
# reader = geoip2.database.Reader(MMDB_PATH)

# def lookup_ip(ip):
#     try:
#         response = reader.city(ip)
#         return {
#             "country": response.country.name,
#             "country_code": response.country.iso_code,
#             "continent": response.continent.name
#         }
#     except geoip2.errors.AddressNotFoundError:
#         return None
#     except Exception as e:
#         print("IP lookup error:", e)
#         return None


import pandas as pd
import ipaddress
from functools import lru_cache

CSV_PATH = "/home/ubuntu/ipinfo/ipinfo_lite.csv"

@lru_cache(maxsize=1)
def load_ipinfo_csv():
    """Load CSV once and cache it"""
    df = pd.read_csv(CSV_PATH)
    # Convert network column to ip_network objects for fast checking
    df['network_obj'] = df['network'].apply(lambda x: ipaddress.ip_network(x))
    return df

def lookup_ip(ip):
    """Return geo info from IPinfo CSV"""
    try:
        ip_obj = ipaddress.ip_address(ip)
    except ValueError:
        return None

    df = load_ipinfo_csv()

    # Find the row where the IP belongs to the network
    for _, row in df.iterrows():
        if ip_obj in row['network_obj']:
            return {
                "country": row['country'],
                "country_code": row['country_code'],
                "continent": row['continent'],
                "continent_code": row['continent_code'],
                "asn": row['asn'],
                "as_name": row['as_name'],
                "as_domain": row['as_domain'],
            }
    return None
