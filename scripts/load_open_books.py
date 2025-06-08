import requests
import re
import os
from urllib.parse import urlencode
from io import BytesIO
from typing import List, Optional, Tuple, Dict, Any

HTTP_HEADERS = {"User-Agent": "LennyImportBot/1.0"}
OPENLIBRARY_SEARCH_URL = "https://openlibrary.org/search.json"
STANDARDEBOOKS_BASE_URL = "https://standardebooks.org/ebooks"
LENNY_PORT = os.getenv("LENNY_PORT")  
LENNY_HOST = os.getenv("LENNY_HOST")
UPLOAD_API_URL = f"http://{LENNY_HOST}:{LENNY_PORT}/v1/api/upload"

class OpenLibrary:
    HTTP_TIMEOUT = 5

    @staticmethod
    def extract_olid(book: Dict[str, Any]) -> Optional[int]:
        try:
            ol_key = book['editions']['docs'][0]['key']
            match = re.search(r"/OL(\d+)[MW]$", ol_key)
            return int(match.group(1)) if match else None
        except KeyError as e:
            return None

    @staticmethod
    def extract_standardebooks_id(book: Dict[str, Any]) -> str:
        try:
            return book['id_standard_ebooks'][0]
        except KeyError as e:
            return None

    @classmethod
    def _construct_search_url(cls, query: str, fields: Optional[List[str]] = None, page: int = 1, limit: int = 100) -> str:
        params = {
            'q': query,
            'fields': ','.join(fields) if fields else '*',
            'page': page,
            'limit': limit
        }
        return f"{OPENLIBRARY_SEARCH_URL}?{urlencode(params)}"

    @classmethod
    def search(cls, query: str, fields: Optional[List[str]] = None, page: int = 1, limit: int = 100) -> Tuple[List[Dict[str, Any]], int]:
        url = cls._construct_search_url(query, fields, page, limit)
        try:
            response = requests.get(url, headers=HTTP_HEADERS, timeout=cls.HTTP_TIMEOUT)
            response.raise_for_status()
            data = response.json()
            return data.get('docs', []), page + 1
        except (requests.exceptions.RequestException, ValueError) as e:
            print(f"Error searching Open Library: {e}")
            return [], page

    @classmethod
    def search_standardebooks(cls) -> List[Dict[str, Any]]:
        query = 'id_standard_ebooks:*'
        fields = ['key', 'id_standard_ebooks', 'editions']

        page = 1
        while True:
            docs, page = OpenLibrary.search(query, fields=fields, page=page, limit=100)
            if not docs:
                break
            yield from docs

class StandardEbooks:
    BASE_URL = "https://standardebooks.org/ebooks"
    HTTP_TIMEOUT = 5

    EPUB_HEADER = b'PK\x03\x04'
    
    @classmethod
    def construct_download_url(cls, identifier: str) -> str:
        identifier_path = identifier
        identifier_file = identifier.replace("/", "_")
        return f"{cls.BASE_URL}/{identifier_path}/downloads/{identifier_file}.epub?source=download"

    @classmethod
    def verify_download(cls, content):
        if content and content.getbuffer().nbytes and content.read(4).startswith(cls.EPUB_HEADER):
            content.seek(0)
            return content
        return None

    @classmethod
    def download(cls, identifier: str, timeout: Optional[int] = None) -> Optional[BytesIO]:
        url = cls.construct_download_url(identifier)
        try:
            response = requests.get(
                url,
                headers=HTTP_HEADERS,
                allow_redirects=True,
                timeout=timeout or cls.HTTP_TIMEOUT,
                stream=True
            )
            response.raise_for_status()
            content = BytesIO()
            for chunk in response.iter_content(chunk_size=8192):
                content.write(chunk)
            content.seek(0)
            return content
        except requests.exceptions.RequestException as e:
            print(f"Error downloading {url}: {e}")
            return None

class Lenny:

    @classmethod
    def upload(cls, olid: int, file_content: BytesIO, encrypted: bool = False,  timeout: int = 120) -> bool:
        data_payload = {
            'openlibrary_edition': olid,
            'encrypted': str(encrypted).lower()
        }
        files_payload = {
            'file': ('book.epub', file_content, 'application/epub+zip')
        }
        try:
            print(UPLOAD_API_URL)
            response = requests.post(
                UPLOAD_API_URL,
                data=data_payload,
                files=files_payload,
                headers=HTTP_HEADERS,
                timeout=timeout,
                verify=False
            )
            response.raise_for_status()
            return True
        except requests.exceptions.RequestException as e:
            print(f"Error uploading to Lenny (OLID: {olid}): {e}")
            return False

def import_standardebooks():
    print("[Preloading] Fetching StandardEbooks from Open Library...")
    docs = OpenLibrary.search_standardebooks()
    for doc in docs:
        olid = OpenLibrary.extract_olid(doc)
        sid = OpenLibrary.extract_standardebooks_id(doc)
        if olid and sid:
            epub = StandardEbooks.download(sid)
            if StandardEbooks.verify_download(epub):
                Lenny.upload(olid, epub, encrypted=False)

if __name__ == "__main__":
    requests.packages.urllib3.disable_warnings(requests.packages.urllib3.exceptions.InsecureRequestWarning)
    import_standardebooks()
