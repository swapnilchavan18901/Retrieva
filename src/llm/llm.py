from fastapi import HTTPException
from openai import AsyncOpenAI
from src.env import env
from src.llm.prompt.query_rewriter_prompt import query_rewriter_prompt
import json

client = AsyncOpenAI(
    api_key=env.SARVAM_API_KEY,
    base_url=env.SARVAM_BASE_URL,
)

async def llm_api_call(system_prompt: str, bot_prompt: str, user_prompt: str, context: list[dict]) -> dict:
    """
    Make an API call to Sarvam with system and user prompts.

    Args:
        system_prompt: The system prompt to guide the model's behavior
        bot_prompt: Bot-specific instructions
        user_prompt: The user's input/query
        context: Document context for the query

    Returns:
        dict: The API response
    """

    # --- Build structured system prompt ---
    context_block = "\n\n".join(
        f"[Source: {chunk.get('file_name', 'unknown')}, Page: {chunk.get('page', 'N/A')}]\n{chunk.get('text', '')}"
        for chunk in context
    ) if context else "No relevant context found."

    final_prompt = (
        f"{system_prompt}\n\n"
        f"## Bot-Specific Instructions\n"
        f"Follow these instructions strictly when responding:\n{bot_prompt}\n\n"
        f"## Context (from uploaded documents)\n"
        f"Use ONLY the following excerpts to answer the user's question.\n\n"
        f"{context_block}"
    )

    try:
        # Use proper role-based message structure
        response = await client.chat.completions.create(
            model=env.SARVAM_MODEL,
            messages=[
                {"role": "system", "content": final_prompt},
                {"role": "user", "content": user_prompt}
            ]
        )

        # Convert response to dict format
        data = response.model_dump()
        print(f"dataishere{data}")
        return data
    except Exception as e:
        print(f"errorishere{e}")
        raise HTTPException(status_code=500, detail=str(e))

async def query_rewriter(query: str) -> dict[str, list[str]]:
    """
    Rewrite the query to be more specific and accurate.
    """
    try:
        response = await client.chat.completions.create(
                model=env.SARVAM_MODEL,
                messages=[
                    {"role": "system", "content": query_rewriter_prompt + (
                        '\nOutput format override: return a JSON object with the key '
                        '"new_queries" containing an array of 4 query strings.'
                    )},
                    {"role": "user", "content": query},
                ],
                temperature=1.0,
                response_format={"type": "json_object"},
            )


        result = json.loads(response.choices[0].message.content)
        queries = result.get("new_queries") if isinstance(result, dict) else None
        if not isinstance(queries, list) or not queries or not all(
            isinstance(item, str) and item.strip() for item in queries
        ):
            raise ValueError("Sarvam returned invalid rewritten queries")
        return {"new_queries": queries}
    except Exception as e:
        print(f"errorishere{e}")
        raise HTTPException(status_code=500, detail=str(e))
