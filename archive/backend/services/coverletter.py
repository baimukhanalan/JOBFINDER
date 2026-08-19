import anthropic

from backend.config import settings
from backend.models.job import Job


async def generate_cover_letter(job: Job, resume_summary: str = "") -> str:
    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

    prompt = f"""Write a professional cover letter for this job position.

Job Title: {job.title}
Company: {job.company}
Description: {job.description or 'Not available'}

{"Candidate Summary: " + resume_summary if resume_summary else ""}

Requirements:
- Professional but not robotic
- Highlight relevant experience
- Show enthusiasm for the role
- Keep it under 300 words
- Don't use cliches like "I am writing to express my interest"
- Start with something engaging about the company or role
"""

    message = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1000,
        messages=[{"role": "user", "content": prompt}],
    )

    return message.content[0].text
