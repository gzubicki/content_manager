from django import forms


def validate_text_for_channel(text, channel):
    text = text or ""
    if channel and len(text) > channel.max_chars:
        raise forms.ValidationError(f"Za długie (> {channel.max_chars} znaków)")


def validate_post_text_for_channel(post):
    validate_text_for_channel(post.text, post.channel)
