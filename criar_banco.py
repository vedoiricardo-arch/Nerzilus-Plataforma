from Nerzilus import app, bootstrap_database


with app.app_context():
    bootstrap_database()
