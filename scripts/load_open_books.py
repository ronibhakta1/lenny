import requests
import re
import os
import time
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

class StandardEbooks:
    BASE_URL = "https://standardebooks.org/ebooks"
    HTTP_TIMEOUT = 5

    @classmethod
    def construct_download_url(cls, identifier: str) -> str:
        identifier_path = identifier
        identifier_file = identifier.replace("/", "_")
        return f"{cls.BASE_URL}/{identifier_path}/downloads/{identifier_file}.epub?source=download"

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

    @classmethod
    def verify_url(cls, identifier: str) -> bool:
        url = cls.construct_download_url(identifier)
        try:
            response = requests.head(url, headers=HTTP_HEADERS, timeout=cls.HTTP_TIMEOUT, allow_redirects=True)
            return response.status_code == 200
        except requests.exceptions.RequestException:
            return False

class Lenny:
    @classmethod
    def upload(cls, olid: int, file_content: BytesIO, encrypted: bool = False) -> bool:
        data_payload = {
            'openlibrary_edition': olid,
            'encrypted': str(encrypted).lower()
        }
        files_payload = {
            'file': ('book.epub', file_content, 'application/epub+zip')
        }
        try:
            response = requests.post(
                UPLOAD_API_URL,
                data=data_payload,
                files=files_payload,
                headers=HTTP_HEADERS,
                timeout=120,
                verify=False
            )
            response.raise_for_status()
            return True
        except requests.exceptions.RequestException as e:
            print(f"Error uploading to Lenny (OLID: {olid}): {e}")
            return False

def extract_edition_number(ol_key: Optional[str]) -> Optional[int]:
    if not ol_key:
        return None
    match = re.search(r"/OL(\d+)[MW]$", ol_key)
    return int(match.group(1)) if match else None

def get_standardebooks_via_openlibrary() -> List[Dict[str, Any]]:
    query = 'id_standard_ebooks:*'
    fields = ['key', 'title', 'id_standard_ebooks', 'editions']
    books = []
    page = 1
    while True:
        docs, next_page = OpenLibrary.search(query, fields=fields, page=page, limit=100)
        if not docs:
            break
        books.extend(docs)
        page = next_page
        time.sleep(0.1)  # Rate limiting
    return books

def process_book(book: Dict[str, Any]) -> bool:
    title = book.get('title', 'Unknown Title')
    standard_ebook_ids = book.get('id_standard_ebooks', [])
    if not standard_ebook_ids:
        print(f"No Standard Ebooks ID for book: {title}")
        return False

    sid = standard_ebook_ids[0]
    if not StandardEbooks.verify_url(sid):
        print(f"Invalid Standard Ebooks URL for ID: {sid}")
        return False

    ol_key = None
    editions_data = book.get('editions', {})
    edition_docs = editions_data.get('docs', [])
    if edition_docs and isinstance(edition_docs, list) and len(edition_docs) > 0:
        ol_key = edition_docs[0].get('key')
    if not ol_key:
        ol_key = book.get('key')
    if not ol_key:
        print(f"No Open Library key for book: {title}")
        return False

    olid = extract_edition_number(ol_key)
    if olid is None:
        print(f"Invalid Open Library key format for book: {title}")
        return False

    file_content = StandardEbooks.download(sid)
    if not file_content:
        print(f"Failed to download book: {title}")
        return False

    file_content.seek(0)
    if file_content.getbuffer().nbytes == 0:
        print(f"Downloaded file is empty for book: {title}")
        return False
    file_content.seek(0)
    if not file_content.read(4).startswith(b'PK\x03\x04'): 
        print(f"Downloaded file is not a valid EPUB for book: {title}")
        return False

    file_content.seek(0)
    if Lenny.upload(olid, file_content, encrypted=False):
        print(f"Successfully uploaded book: {title} (OLID: {olid})")
        return True
    return False

def import_standardebooks():
    uploaded_books_count = 0
    print("Starting to load books from Open Library...")
    books = get_standardebooks_via_openlibrary()
    print(f"Found {len(books)} books to process.")
    
    for book in books:
        if process_book(book):
            uploaded_books_count += 1
        time.sleep(0.1) 
    
    print(f"Total books successfully uploaded: {uploaded_books_count}")

if __name__ == "__main__":
    requests.packages.urllib3.disable_warnings(requests.packages.urllib3.exceptions.InsecureRequestWarning)
    import_standardebooks()