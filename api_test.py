import requests
import json

def test_api_call(search_term):
    """Tests the new Chub API search endpoint."""
    url = f"https://api.chub.ai/search?search={search_term}"
    print(f"Testing API with URL: {url}")
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        response = requests.get(url, headers=headers)
        response.raise_for_status()  # Raise an exception for HTTP errors (4xx or 5xx)
        data = response.json()

        print("\nAPI Response (raw):")
        print(json.dumps(data, indent=2))

        if 'data' in data and 'nodes' in data['data'] and data['data']['nodes']:
            cards_data = data['data']['nodes']
            print(f"\nFound {len(cards_data)} card(s) for '{search_term}':")
            for i, card in enumerate(cards_data):
                name = card.get('name', 'N/A')
                full_path = card.get('fullPath', 'N/A')
                tagline = card.get('tagline', 'N/A')
                avatar_url = card.get('avatar_url', 'N/A')
                
                print(f"\n--- Card {i+1} ---")
                print(f"  Name: {name}")
                print(f"  Full Path: {full_path}")
                print(f"  Tagline: {tagline}")
                print(f"  Avatar URL: {avatar_url}")
        else:
            print(f"No cards found for '{search_term}'.")

    except requests.exceptions.RequestException as e:
        print(f"Error during API call: {e}")
    except json.JSONDecodeError:
        print("Error decoding JSON response.")
        print(f"Response text: {response.text}")

if __name__ == "__main__":
    search_query = "Sabrina"  # Example search term
    test_api_call(search_query)
