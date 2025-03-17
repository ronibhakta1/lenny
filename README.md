# Lenny

![lenny](https://github.com/user-attachments/assets/b7d70bf8-d795-419c-97b1-5cf4f9bad3f9)

Lenny is a free, open source Lending System for Libraries.

## Technologies

* [`docker`](https://www.docker.com/) for deployment and containerization
* [`nginx`](https://nginx.org/) handles patron requests (reverse proxy to app)
* [`flask`](https://flask.palletsprojects.com/en/stable/) (python) as the web & API microframework
* `s3-like` API for storing digital assets (Amazon, Internet Archive, or local flat-file)
* [`yaml`](https://en.wikipedia.org/wiki/YAML/) for configuring library-level rules
* [`postgres`](https://www.postgresql.org/) for the database
* [`SQLAlchemy`](https://www.sqlalchemy.org/) for the database python [`ORM`](https://en.wikipedia.org/wiki/Object%E2%80%93relational_mapping)
* [Readium `LCP`](https://readium.org/lcp-specs/) for [DRM](https://en.wikipedia.org/wiki/Digital_rights_management); see [LCP Server](https://github.com/readium/readium-lcp-server)
* [Readium Web SDK](https://www.edrlab.org/software/readium-web/) for a secure web reading experience
* [`OPDS`](https://en.wikipedia.org/wiki/Open_Publication_Distribution_System) RSS-like standard for syndicating holdings

## Endpoints

* `/v{1}/api`
* `/v{1}/manage`
* `/v{1}/read`
* `/v{1}/opds`
* `/v{1}/stats`

## Pilot

We're seeking partnerships with libraries who would like to try lending digital resources to their patrons. 

## Open Topics

* Authentication - How does your library perform authentication currently?

