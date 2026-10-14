# ceur-paperless

Paperless-ngx as fifth platform of the CEUR-WS approach comparison, see
[issue 1](https://github.com/WolfgangFahl/ceur-paperless/issues/1).

Imports ranges of CEUR-WS volumes from a local mirror into a Paperless-ngx instance:
one document per paper PDF, title and created date from
[ceur-spt](https://github.com/ceurws/ceur-spt), a tag per volume and the custom fields
`ceur_id`, `ceur_volume`, `authors`, `pages`, `pdf_url`.

## Install
```bash
pip install -e .
```

## Usage
The API token is read from stdin.
```bash
ceur-paperless --mirror /path/to/ceur-ws --viewer viewer 3850-3899 < token.txt
```
