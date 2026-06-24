Place this project image file here:

- `logo.jpg` - your league logo. It is used in the header and rotating loading screen.

The public home pages now use a CSS-only hero layout, so no homepage image file is required.

The database seed stores the public logo URL in `app_assets`:

- `league_logo` -> `/static/images/logo.jpg`

You can replace the logo file without changing the templates. If you host the logo in Supabase Storage later, update the matching `app_assets.url` value to the Supabase public URL.
