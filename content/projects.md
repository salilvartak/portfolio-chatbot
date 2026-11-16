# Projects

## GLX 2.0 (Optimus Travels), work project
<!-- ask: Tell me about GLX 2.0 -->
Stack: React, TypeScript, Python, FastAPI, PostgreSQL, Helm.
- Multi-tenant B2B/B2C travel management platform: three independently deployable services (admin API, partner API, React admin panel) sharing one PostgreSQL database with 81 migrations.
- Built and extended booking flows in the partner API (creation, updates, reporting sync and payments across flight, hotel and cab bookings) while keeping partner integrations backward compatible.
- Implemented reusable traveler profiles and OTP-based authentication for B2C users, with token-based login shared across services.
- Contributed to CI/CD: GitLab pipelines, Helm-based AKS deployments and pre-upgrade migration jobs across dev, staging and production.

## SBX Platform, work project
<!-- ask: Tell me about the SBX platform -->
Stack: Python, FastAPI, LangGraph, PostgreSQL, Docker, Azure.
- Travel-ticketing extension sold by ProDT; delivered DSR (daily sales report) integration across flight, hotel, cab, bus and train bookings.
- Built a LangGraph AI pipeline that extracts data from PDF invoices and turns it into structured, actionable data.
- Migrated the data layer to PostgreSQL with async SQLAlchemy and Alembic-managed migrations.

## Application Backend, work project
<!-- ask: Tell me about his real-time chat backend -->
Stack: Python, FastAPI.
- Backend for user verification and real-time chat supporting 500 concurrent users, using JWT auth and WebSockets.

## SKOR AI Dashboard and Agent Hunter, work project
<!-- ask: Tell me about the SKOR AI dashboard -->
Stack: React.js, Node.js, Firebase.
- Real-time gaming analytics dashboard plus a tournament discovery platform for esports, processing 5,000+ data points per minute.

## Morex Asset Management Website, work project
<!-- ask: Tell me about the Morex website -->
Stack: React.js, Salesforce, CMS.
- Multipage marketing site with CMS editing and Salesforce integration, tracking 60+ incoming leads per month.

## Side projects
<!-- ask: What side projects has he built? -->
- Udhari: Flutter app for tracking informal loans; Firebase Firestore, Google OAuth, OCR import via Google ML Kit.
- Clush: Flutter dating app with a custom color system and an animated "Gem" super-like.
- Anvil: VS Code-like mobile IDE for Android, built with Flutter and a Monaco WebView.
- SpaceBridgeX: Chrome extension with a CI/CD publishing pipeline and analytics.
- Teams AI bot: FastAPI + Azure Bot Framework + Claude API for auto-replies in Microsoft Teams.
- Automated reporting bot: Python + Power Automate + GitHub Actions, posting daily booking/feedback summaries to Teams from Azure PostgreSQL.
- This portfolio chatbot: a RAG assistant built with Python, FastAPI, LangGraph, Gemini and Firestore vector search.
