# Extended Flask App for MedTrack with AWS DynamoDB and SNS Integration
from flask import Flask, render_template, request, redirect, url_for, flash, session
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime
import logging
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import os
import uuid
from dotenv import load_dotenv
import boto3
from boto3.dynamodb.conditions import Key, Attr

# Load environment variables
load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "default_secret_key")

AWS_REGION = os.getenv("AWS_REGION")
dynamodb = boto3.resource('dynamodb', region_name=AWS_REGION)
sns = boto3.client('sns', region_name=AWS_REGION)

users_table = dynamodb.Table(os.getenv("DYNAMODB_TABLE_USERS"))
appointments_table = dynamodb.Table(os.getenv("DYNAMODB_TABLE_APPOINTMENTS"))
SNS_TOPIC_ARN = os.getenv("SNS_TOPIC_ARN")

@app.context_processor
def inject_now():
    return {'now': datetime.now()}

# ---------- Helper: Send Email ----------
def send_email(to_email, subject, message):
    from_email = os.getenv("SMTP_EMAIL")
    password = os.getenv("SMTP_PASSWORD")

    msg = MIMEMultipart()
    msg['From'] = from_email
    msg['To'] = to_email
    msg['Subject'] = subject
    msg.attach(MIMEText(message, 'html'))

    try:
        with smtplib.SMTP_SSL(os.getenv("SMTP_SERVER"), int(os.getenv("SMTP_PORT"))) as server:
            server.login(from_email, password)
            server.send_message(msg)
        logging.info(f"Email sent to {to_email}")
    except Exception as e:
        logging.error(f"Email failed to send: {e}")

# ---------- Helper: Send SNS Notification ----------
def notify_sns(subject, message):
    try:
        sns.publish(
            TopicArn=SNS_TOPIC_ARN,
            Message=message,
            Subject=subject
        )
        logging.info("SNS notification sent.")
    except Exception as e:
        logging.error(f"SNS notification failed: {e}")

# ---------- Custom Error Handlers ----------
@app.errorhandler(404)
def page_not_found(e):
    return render_template("404.html"), 404

@app.errorhandler(500)
def internal_server_error(e):
    return render_template("500.html"), 500


# ---------- Routes ----------
@app.route('/')
def home():
    return render_template('index.html')

@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        user_type = request.form.get('userType')
        name = request.form.get('name')
        email = request.form.get('email')
        password = request.form.get('password')
        confirm_password = request.form.get('confirm_password')

        if not all([user_type, name, email, password, confirm_password]):
            flash("Please fill in all required fields.", "danger")
            return redirect(url_for('signup'))

        if password != confirm_password:
            flash("Passwords do not match.", "danger")
            return redirect(url_for('signup'))

        response = users_table.get_item(Key={'email': email})
        if 'Item' in response:
            flash("Email already registered.", "danger")
            return redirect(url_for('signup'))

        user = {
            'id': str(uuid.uuid4()),
            'type': user_type,
            'name': name,
            'email': email,
            'password': generate_password_hash(password)
        }

        if user_type == 'patient':
            user.update({
                'age': request.form.get('patient_age'),
                'gender': request.form.get('patient_gender'),
                'address': request.form.get('address'),
                'mobile': request.form.get('mobile')
            })
        elif user_type == 'doctor':
            age = int(request.form.get('doctor_age', 0))
            if age < 23:
                flash("Doctor must be at least 23 years old.", "danger")
                return redirect(url_for('signup'))
            user.update({
                'age': str(age),
                'gender': request.form.get('doctor_gender'),
                'specialization': request.form.get('specialization'),
                'mobile': request.form.get('mobile')
            })

        users_table.put_item(Item=user)
        flash("Signup successful. Please log in.", "success")
        return redirect(url_for('login'))

    return render_template('signup.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        role = request.form.get('role')
        email = request.form.get('email')
        password = request.form.get('password')

        response = users_table.get_item(Key={'email': email})
        user = response.get('Item')

        if user and check_password_hash(user['password'], password) and user['type'] == role:
            session['user'] = user['email']
            session['role'] = user['type']
            flash("Login successful!", "success")
            return redirect(url_for(f"{role}_dashboard"))

        flash("Invalid credentials or role mismatch.", "danger")
        return redirect(url_for('login'))

    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    flash("Logged out successfully.", "success")
    return redirect(url_for('login'))

@app.route('/patient/dashboard')
def patient_dashboard():
    if 'user' not in session or session.get('role') != 'patient':
        flash("Please log in as a patient.", "danger")
        return redirect(url_for('login'))

    patient_email = session['user']
    user = users_table.get_item(Key={'email': patient_email}).get('Item')
    appts = appointments_table.scan(FilterExpression=Attr('patient_email').eq(patient_email))['Items']
    pending = sum(1 for a in appts if a['status'] == 'Pending')
    completed = sum(1 for a in appts if a['status'] == 'Completed')

    doctors = users_table.scan(FilterExpression=Attr('type').eq('doctor'))['Items']

    return render_template('patient_dashboard.html', user=user, appointments=appts, doctors=doctors, pending=pending, completed=completed, total=len(appts))

@app.route('/doctor/dashboard')
def doctor_dashboard():
    if 'user' not in session or session.get('role') != 'doctor':
        flash("Please log in as a doctor.", "danger")
        return redirect(url_for('login'))

    doctor_email = session['user']
    user = users_table.get_item(Key={'email': doctor_email}).get('Item')
    appts = appointments_table.scan(FilterExpression=Attr('doctor_id').eq(doctor_email))['Items']
    pending = sum(1 for a in appts if a['status'] == 'Pending')
    completed = sum(1 for a in appts if a['status'] == 'Completed')

    return render_template('doctor_dashboard.html', user=user, appointments=appts, pending=pending, completed=completed, total=len(appts))


# ---------- Additional Routes for Appointments ----------
@app.route('/book-appointment', methods=['GET', 'POST'])
def book_appointment():
    if 'user' not in session or session.get('role') != 'patient':
        flash("Please log in as a patient to book an appointment.", "danger")
        return redirect(url_for('login'))

    if request.method == 'POST':
        doctor_email = request.form.get('doctor_id')
        date = request.form.get('appointment_date')
        time = request.form.get('appointment_time')
        symptoms = request.form.get('symptoms', '')

        appointment = {
            'appointment_id': str(uuid.uuid4()),
            'patient_email': session['user'],
            'doctor_id': doctor_email,
            'date': date,
            'time': time,
            'status': 'Pending',
            'symptoms': symptoms,
            'created_at': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }

        appointments_table.put_item(Item=appointment)

        doctor_response = users_table.get_item(Key={'email': doctor_email})
        doctor = doctor_response.get('Item')

        subject = "New Appointment Booked"
        message = f"<h3>New Appointment</h3><p>You have a new appointment on {date} at {time}.<br>Symptoms: {symptoms}</p>"
        if doctor and 'email' in doctor:
            send_email(doctor['email'], subject, message)
            notify_sns(subject, f"Doctor {doctor['name']} has a new appointment from {session['user']} on {date} at {time}.")

        flash("Appointment booked successfully! Notification sent to doctor.", "success")
        return redirect(url_for('patient_dashboard'))

    doctor_dict = users_table.scan(FilterExpression=Attr('type').eq('doctor'))['Items']
    return render_template('book_appointment.html', doctors=doctor_dict)

@app.route('/view-appointment/<appointment_id>')
def view_appointment_patient(appointment_id):
    response = appointments_table.scan(FilterExpression=Attr('appointment_id').eq(appointment_id))
    items = response.get('Items', [])
    if not items:
        flash("Appointment not found.", "danger")
        return redirect(url_for('patient_dashboard'))

    appt = items[0]
    if session.get('user') != appt['patient_email']:
        flash("Access denied.", "danger")
        return redirect(url_for('patient_dashboard'))

    doctor = users_table.get_item(Key={'email': appt['doctor_id']}).get('Item')
    return render_template("view_appointment_patient.html", appointment=appt, doctor=doctor)

@app.route('/doctor/view-appointment/<appointment_id>')
def view_appointment_doctor(appointment_id):
    response = appointments_table.scan(FilterExpression=Attr('appointment_id').eq(appointment_id))
    items = response.get('Items', [])
    if not items:
        flash("Appointment not found.", "danger")
        return redirect(url_for('doctor_dashboard'))

    appt = items[0]
    patient = users_table.get_item(Key={'email': appt['patient_email']}).get('Item')
    return render_template("view_appointment_doctor.html", appointment=appt, patient=patient)

@app.route('/doctor/submit-diagnosis/<appointment_id>', methods=['POST'])
def submit_diagnosis(appointment_id):
    if 'user' not in session or session.get('role') != 'doctor':
        flash("Unauthorized access.", "danger")
        return redirect(url_for('login'))

    diagnosis = request.form.get('diagnosis')
    treatment_plan = request.form.get('treatment_plan')
    prescription = request.form.get('prescription')

    response = appointments_table.scan(FilterExpression=Attr('appointment_id').eq(appointment_id))
    items = response['Items']
    if items:
        appointment = items[0]
        appointment.update({
            'diagnosis': diagnosis,
            'treatment_plan': treatment_plan,
            'prescription': prescription,
            'status': 'Completed'
        })
        appointments_table.put_item(Item=appointment)
        flash("Diagnosis submitted successfully.", "success")
    else:
        flash("Appointment not found.", "danger")

    return redirect(url_for('doctor_dashboard'))

@app.route('/patient/profile')
def patient_profile():
    email = session.get('user')
    if not email or session.get('role') != 'patient':
        flash("Unauthorized access.", "danger")
        return redirect(url_for('login'))
    user = users_table.get_item(Key={'email': email}).get('Item')
    return render_template("patient_profile.html", user=user)

@app.route('/doctor/profile')
def doctor_profile():
    email = session.get('user')
    if not email or session.get('role') != 'doctor':
        flash("Unauthorized access.", "danger")
        return redirect(url_for('login'))
    user = users_table.get_item(Key={'email': email}).get('Item')
    return render_template("doctor_profile.html", user=user)

@app.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    if request.method == 'POST':
        email = request.form.get('email')
        response = users_table.get_item(Key={'email': email})
        user = response.get('Item')
        if user:
            flash("Password reset link sent (simulated).", "success")
        else:
            flash("Email not found.", "danger")
        return redirect(url_for('login'))
    return render_template('forgot_password.html')

if __name__ == '__main__':
    app.run(debug=True)
