import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from app.auth import hash_password
from app.db import SessionLocal, create_tables
from app.models import PanelUser


def main() -> None:
    parser = argparse.ArgumentParser(description="Crea o actualiza un usuario del panel de administracion.")
    parser.add_argument("--nombre", required=True)
    parser.add_argument("--email", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--rol", default="Administrador")
    args = parser.parse_args()

    create_tables()
    with SessionLocal() as session:
        user = session.scalar(select(PanelUser).where(PanelUser.email == args.email))
        if user is None:
            user = PanelUser(nombre=args.nombre, email=args.email, rol=args.rol)
            session.add(user)
        else:
            user.nombre = args.nombre
            user.rol = args.rol
        user.password_hash = hash_password(args.password)
        user.activo = True
        session.commit()
        print(f"Usuario de panel listo: {args.email}")


if __name__ == "__main__":
    main()
