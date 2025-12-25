import asyncio
import aiohttp
import os
from dotenv import load_dotenv

load_dotenv()

AI_API_KEY = os.getenv("AI_API_KEY", "")
AI_API_URL = "https://api.openai.com/v1/chat/completions"

async def test_openai_api():
    """Test if OpenAI API key is valid"""
    if not AI_API_KEY:
        print("❌ AI_API_KEY not found in .env")
        return False
    
    print(f"🔑 Testing API key: {AI_API_KEY[:20]}...")
    
    try:
        async with aiohttp.ClientSession() as session:
            headers = {
                "Authorization": f"Bearer {AI_API_KEY}",
                "Content-Type": "application/json"
            }
            
            data = {
                "model": "gpt-3.5-turbo",
                "messages": [
                    {"role": "system", "content": "You are a helpful assistant."},
                    {"role": "user", "content": "Say 'API works!'"}
                ],
                "temperature": 0.7,
                "max_tokens": 50
            }
            
            print(f"📡 Sending request to {AI_API_URL}...")
            
            async with session.post(AI_API_URL, headers=headers, json=data) as response:
                status = response.status
                print(f"📊 Response status: {status}")
                
                if status == 200:
                    result = await response.json()
                    ai_message = result['choices'][0]['message']['content']
                    print(f"✅ API works! Response: {ai_message}")
                    return True
                else:
                    error_text = await response.text()
                    print(f"❌ API error ({status}): {error_text}")
                    return False
                    
    except Exception as e:
        print(f"❌ Exception: {e}")
        import traceback
        traceback.print_exc()
        return False

if __name__ == "__main__":
    result = asyncio.run(test_openai_api())
    if result:
        print("\n✅ OpenAI API ключ работает!")
    else:
        print("\n❌ OpenAI API ключ НЕ работает или неверный!")
