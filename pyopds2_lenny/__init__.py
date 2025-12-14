from typing import List, Tuple, Optional, cast
from collections.abc import Mapping, Iterable
from pyopds2_openlibrary import OpenLibraryDataProvider, OpenLibraryDataRecord, Link
from pyopds2.provider import DataProvider, DataProviderRecord


class LennyDataRecord(OpenLibraryDataRecord):
    """Extends OpenLibraryDataRecord with local borrow/return links for Lenny."""

    lenny_id: Optional[int] = None
    is_encrypted: bool = False

    @property
    def type(self) -> str:
        return "http://schema.org/Book"

    def links(self) -> List[Link]:
        """Override acquisition links to use Lenny's API endpoints.

        If the record was created with an `is_encrypted` flag the primary
        acquisition link will be `/borrow` (for encrypted/loaned content),
        otherwise `/read` for open-access/readable content. When encrypted
        we also include a `return` endpoint.
        """
        if not self.lenny_id:
            return super().links() or []

        # Minimal, predictable acquisition links: always use Lenny/Read titles
        lenny_links = [
            Link(
                rel="self",
                href=f"{LennyDataProvider.BASE_URL}opds/{self.lenny_id}",
                type="application/opds-publication+json",
                title=None,
                templated=False,
                properties=None,
            )
        ]

        base_uri = f"{LennyDataProvider.BASE_URL}items/{self.lenny_id}"

        if self.is_encrypted:
            borrowable = getattr(self, "is_borrowable", None)
            if borrowable is None:
                # If provider didn't supply, default to available for permissive behavior
                avail_state = "available"
            else:
                avail_state = "available" if bool(borrowable) else "unavailable"

            lenny_links.append(
                Link(
                    href=f"{base_uri}/read",
                    rel="http://opds-spec.org/acquisition/borrow",
                    type="application/opds-publication+json",
                    title="Lenny",
                    templated=False,
                    properties={
                        "authenticate": {
                            "type": "application/opds-authentication+json",
                            "href": f"{LennyDataProvider.BASE_URL}oauth/implicit"
                        },
                        "availability": {"state": avail_state},
                        "indirectAcquisition": [
                            {
                                "type": "application/vnd.readium.lcp.license.v1.0+json",
                                "child": [
                                    {
                                        "type": "application/epub+zip"
                                    }
                                ]
                            }
                        ],
                    },
                )
            )
        else:
            lenny_links.append(
                Link(
                    href=f"{base_uri}/read",
                    rel="http://opds-spec.org/acquisition/open-access",
                    type="application/opds-publication+json",
                    title="Read",
                    templated=False,
                    properties=None,
                )
            )
        return lenny_links



class LennyDataProvider(OpenLibraryDataProvider):
    """Adapts Open Library metadata for Lenny's local catalog."""

    @staticmethod
    def search(
        query: str,
        limit: int = 50,
        offset: int = 0,
        lenny_ids: Optional[Mapping[int, int]] = None,
        encryption_map: Optional[Mapping[int, bool]] = None,
        borrowable_map: Optional[Mapping[int, bool]] = None,
    ) -> DataProvider.SearchResponse:
        """Perform a metadata search and adapt results into LennyDataRecords."""
        resp = OpenLibraryDataProvider.search(query=query, limit=limit, offset=offset)

        lenny_records: List[LennyDataRecord] = []
        if isinstance(lenny_ids, Mapping):
            keys = list(lenny_ids.keys())
            values = list(lenny_ids.values())

            def _looks_like_index_sequence(seq: List[int]) -> bool:
                if not seq or not all(isinstance(item, int) for item in seq):
                    return False
                return seq == list(range(len(seq))) or seq == list(range(1, len(seq) + 1))

            keys_are_indices = _looks_like_index_sequence(keys)
            values_are_indices = _looks_like_index_sequence(values)

            if values and not values_are_indices:
                lenny_id_values = values
            elif keys and not keys_are_indices:
                lenny_id_values = keys
            elif values and not keys:
                lenny_id_values = values
            elif keys:
                lenny_id_values = keys
            else:
                lenny_id_values = []
        elif isinstance(lenny_ids, Iterable) and not isinstance(lenny_ids, (str, bytes)):
            lenny_id_values = list(lenny_ids)
        else:
            lenny_id_values = []

        for idx, record in enumerate(resp.records):
            data = record.model_dump()

            if idx < len(lenny_id_values):
                data["lenny_id"] = lenny_id_values[idx]

            lenny_id = data.get("lenny_id")
            if encryption_map and lenny_id is not None:
                data["is_encrypted"] = encryption_map.get(lenny_id, False)
            else:
                data["is_encrypted"] = False
            if borrowable_map and lenny_id is not None:
                data["is_borrowable"] = bool(borrowable_map.get(lenny_id, False))
            lenny_records.append(LennyDataRecord.model_validate(data))
            
        return DataProvider.SearchResponse(
            provider=LennyDataProvider,
            records=cast(List[DataProviderRecord], lenny_records),
            total=resp.total,
            query=resp.query,
            limit=resp.limit,
            offset=resp.offset,
            sort=resp.sort,
        )

    @classmethod
    def get_authentication_document(cls) -> dict:
        """
        Returns the OPDS Authentication Document (JSON).
        Uses cls.BASE_URL which should be set by the application.
        """
        # Ensure BASE_URL ends with slash or handle it.
        # Based on usage in properties/links, it seems to end with slash.
        base = cls.BASE_URL
        
        return {
            "id": f"{base}oauth/implicit",
            "title": "Lenny Authentication",
            "description": "Sign in to Lenny",
            "authentication": [
                {
                    "type": "http://opds-spec.org/auth/oauth/implicit",
                    "links": [
                        {
                            "rel": "authenticate",
                            "href": f"{base}oauth/authorize",
                            "type": "text/html"
                        }
                    ]
                }
            ],
            "links": [
                 {
                    "rel": "profile",
                    "href": f"{base}profile",
                    "type": "application/opds-profile+json"
                 },
                 {
                    "rel": "http://opds-spec.org/shelf",
                    "href": f"{base}profile",
                    "type": "application/opds+json"
                 },
                 {
                    "rel": "start",
                    "href": f"{base}opds",
                    "type": "application/opds+json"
                 }
            ]
        }