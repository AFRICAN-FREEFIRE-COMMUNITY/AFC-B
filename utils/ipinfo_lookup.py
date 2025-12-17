import geoip2.database
import os

# Path to your MMDB file
MMDB_PATH = "/home/ubuntu/ipinfo/ipinfo_lite.mmdb"

reader = geoip2.database.Reader(MMDB_PATH)

def lookup_ip(ip):
    """
    Returns country, country_code, and continent from an IP
    """
    try:
        response = reader.city(ip)
        return {
            "response": response,
        }
        # return {
        #     "country": response.country.name,
        #     "country_code": response.country.iso_code,
        #     "continent": response.continent.name
        # }
    except geoip2.errors.AddressNotFoundError:
        # IP not in database
        return None
    except Exception as e:
        print("IP lookup error:", e)
        return None
