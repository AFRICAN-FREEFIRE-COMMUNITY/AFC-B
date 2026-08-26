"""Per-provider adapters. Each module exposes `normalize(profile) -> dict` with the house keys
provider_user_id / username / email / avatar_url / raw_profile, plus anything that provider alone
needs. Keeping the shape identical is what lets links.link_account() be provider-agnostic."""
