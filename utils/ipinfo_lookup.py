import geoip2.database

MMDB_PATH = "/home/ubuntu/ipinfo/ipinfo_lite.mmdb"
reader = geoip2.database.Reader(MMDB_PATH)

def lookup_ip(ip):
    try:
        response = reader.city(ip)
        return {
            "country": response.country.name,
            "country_code": response.country.iso_code,
            "continent": response.continent.name
        }
    except geoip2.errors.AddressNotFoundError:
        return None
    except Exception as e:
        print("IP lookup error:", e)
        return None
