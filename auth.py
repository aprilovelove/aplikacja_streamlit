import bcrypt
from database import User, SessionLocal

def hash_password(password):
    return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')

def check_password(password, hashed):
    return bcrypt.checkpw(password.encode('utf-8'), hashed.encode('utf-8'))

def register_user(username, password):
    db = SessionLocal()
    if db.query(User).filter_by(username=username).first():
        db.close()
        return False
    new_user = User(username=username, password=hash_password(password))
    db.add(new_user)
    db.commit()
    db.close()
    return True

def login_user(username, password):
    db = SessionLocal()
    user = db.query(User).filter_by(username=username).first()
    db.close()
    if user and check_password(password, user.password):
        return user
    return None