from Nerzilus import app, database, seed_initial_data


with app.app_context():
    database.create_all()
    seed_initial_data()
