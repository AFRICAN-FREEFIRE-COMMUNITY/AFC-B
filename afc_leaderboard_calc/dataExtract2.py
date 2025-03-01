import openai
import json
import os
from urllib.parse import urlparse
import base64

# Set your OpenAI API key here
openai.api_key = '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'

def load_json_schema(schema_file: str) -> dict:
    with open(schema_file, 'r') as file:
        return json.load(file)

image_path = 'result1.jpg'


# Load JSON schema
score_schema = load_json_schema('test_schema.json')

with open(image_path, 'rb') as image_file:
    image_base64 = base64.b64encode(image_file.read()).decode('utf-8')

# Create the request with the schema and image URL as part of the message
response = openai.ChatCompletion.create(
    model='gpt-4o',
    response_format = {"type": "json_object"},
    messages=[
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": f"Provide a JSON file that represents this document based on the following JSON Schema:\n{json.dumps(score_schema)}\n"
                    
                },
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/png;base64,{image_base64}"
                    }
                }

            ]
        }
    ],
    max_tokens=500
)

# Output the response
# print(response['choices'][0]['message']['content'])

print(response)

json_data = json.loads(response['choices'][0]['message']['content'])
filename_without_extension = os.path.splitext(os.path.basename(urlparse(image_path).path))[0]
json_filename =f"{filename_without_extension}.json"

with open(json_filename, "w") as file:
    json.dump(json_data, file, indent=4)


print(f"JSON data saved to {json_filename}")

