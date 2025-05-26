import requests 

def load_open_books():
    """
    Load open books from the Open Library API and save them to a local JSON file.
    """
    url = "https://openlibrary.org/search.json?q=id_standard_ebooks:*&limit=100"
    response = requests.get(url)
    
    if response.status_code == 200:
        data = response.json()
        return data
    else:
        print(f"Failed to load open books. Status code: {response.status_code}")
        

open_books = load_open_books()
print([book["id_standard_ebooks"] for book in open_books[:100]])
# if open_books and "docs" in open_books:
#     titles = [book.get("title") for book in open_books["docs"] if "title" in book]
#     standard_ebooks = [book.get("id_standard_ebooks") for book in open_books["docs"] if "id_standard_ebooks" in book]
#     # print(f"Found {len(titles)} books in Open Books. Example titles: {titles[:100]}")
#     print(f"Found {len(standard_ebooks)} books with standard ebooks ID. {standard_ebooks[:100]}")
    

