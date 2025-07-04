# Extended Flask App for MedTrack with AWS Enhancements
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

# AWS & App Configs
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
dynamodb = boto3.resource(
    'dynamodb',
    region_name=AWS_REGION,
    aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
    aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY")
)

sns = boto3.client(
    'sns',
    region_name=AWS_REGION,
    aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
    aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY")
)

USERS_TABLE_NAME = os.getenv("DYNAMODB_TABLE_USERS", "UsersTable")
APPOINTMENTS_TABLE_NAME = os.getenv("DYNAMODB_TABLE_APPOINTMENTS", "AppointmentsTable")
user_table = dynamodb.Table(USERS_TABLE_NAME)
appointment_table = dynamodb.Table(APPOINTMENTS_TABLE_NAME)

SNS_TOPIC_ARN = os.getenv("SNS_TOPIC_ARN")
ENABLE_SNS = os.getenv("ENABLE_SNS", "False").lower() == "true"

SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", 465))
SENDER_EMAIL = os.getenv("SMTP_EMAIL")
SENDER_PASSWORD = os.getenv("SMTP_PASSWORD")
ENABLE_EMAIL = os.getenv("ENABLE_EMAIL", "False").lower() == "true"

# Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

# Context processor for templates
@app.context_processor
def inject_now():
    return {'now': datetime.now()}

# Health check for AWS ALB
@app.route('/health')
def health():
    return {'status': 'healthy'}, 200

# Email Helper

def send_email(to_email, subject, message):
    if not ENABLE_EMAIL:
        logger.info(f"[Email Disabled] Skipping email to {to_email}")
        return
    try:
        msg = MIMEMultipart()
        msg['From'] = SENDER_EMAIL
        msg['To'] = to_email
        msg['Subject'] = subject
        msg.attach(MIMEText(message, 'html'))

        with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT) as server:
            server.login(SENDER_EMAIL, SENDER_PASSWORD)
            server.send_message(msg)
        logger.info(f"Email sent to {to_email}")
    except Exception as e:
        logger.error(f"Failed to send email to {to_email}: {e}")

# SNS Helper

def notify_sns(subject, message):
    if not ENABLE_SNS or not SNS_TOPIC_ARN:
        logger.info("[SNS Disabled] Skipping publish")
        return
    try:
        response = sns.publish(
            TopicArn=SNS_TOPIC_ARN,
            Message=message,
            Subject=subject
        )
        logger.info(f"SNS published: {response.get('MessageId')}")
    except Exception as e:
        logger.error(f"Failed to publish SNS: {e}")


# Routes
@app.route('/')
def index():
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

        if user_table.get_item(Key={'email': email}).get('Item'):
            flash("Email already registered.", "danger")
            return redirect(url_for('signup'))

        user = {
            'id': str(uuid.uuid4()),
            'type': user_type,
            'name': name,
            'email': email,
            'password': generate_password_hash(password),
            'created_at': datetime.utcnow().isoformat()
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

        user_table.put_item(Item=user)
        notify_sns("New Signup", f"{user_type} {name} registered with email {email}.")
        send_email(email, "Welcome to MedTrack", f"<p>Hi {name}, welcome to MedTrack!</p>")
        flash("Signup successful. Please log in.", "success")
        return redirect(url_for('login'))

    return render_template('signup.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        role = request.form.get('role')

        user = user_table.get_item(Key={'email': email}).get('Item')
        if user and user['type'] == role and check_password_hash(user['password'], password):
            session['user'] = email
            session['role'] = role
            flash("Login successful!", "success")
            return redirect(url_for(f"{role}_dashboard"))

        flash("Invalid credentials or role.", "danger")
        return redirect(url_for('login'))

    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    flash("Logged out successfully.", "success")
    return redirect(url_for('login'))

@app.route('/patient/dashboard')
def patient_dashboard():
    if session.get('role') != 'patient':
        flash("Unauthorized access.", "danger")
        return redirect(url_for('login'))
    email = session['user']
    user = user_table.get_item(Key={'email': email}).get('Item')
    appointments = appointment_table.scan(FilterExpression=Attr('patient_email').eq(email))['Items']
    doctors = user_table.scan(FilterExpression=Attr('type').eq('doctor'))['Items']
    return render_template('patient_dashboard.html', user=user, appointments=appointments, doctors=doctors)

@app.route('/doctor/dashboard')
def doctor_dashboard():
    if session.get('role') != 'doctor':
        flash("Unauthorized access.", "danger")
        return redirect(url_for('login'))
    email = session['user']
    user = user_table.get_item(Key={'email': email}).get('Item')
    appointments = appointment_table.scan(FilterExpression=Attr('doctor_email').eq(email))['Items']
    return render_template('doctor_dashboard.html', user=user, appointments=appointments)

# ---------- Appointment Routes ----------

@app.route('/book_appointment', methods=['GET', 'POST'])
def book_appointment():
    if not is_logged_in() or session.get('role') != 'patient':
        flash('Only patients can book appointments', 'danger')
        return redirect(url_for('login'))

    if request.method == 'POST':
        doctor_email = request.form.get('doctor_email')
        symptoms = request.form.get('symptoms')
        appointment_date = request.form.get('appointment_date')
        appointment_time = request.form.get('appointment_time')
        patient_email = session.get('email')

        if not doctor_email or not symptoms or not appointment_date or not appointment_time:
            flash('Please fill all fields', 'danger')
            return redirect(url_for('book_appointment'))

        doctor = user_table.get_item(Key={'email': doctor_email}).get('Item')
        patient = user_table.get_item(Key={'email': patient_email}).get('Item')

        appointment_id = str(uuid.uuid4())
        appointment_item = {
            'appointment_id': appointment_id,
            'doctor_email': doctor_email,
            'doctor_name': doctor.get('name', 'Doctor'),
            'patient_email': patient_email,
            'patient_name': patient.get('name', 'Patient'),
            'symptoms': symptoms,
            'status': 'pending',
            'appointment_date': appointment_date,
            'appointment_time': appointment_time,
            'created_at': datetime.utcnow().isoformat()
        }

        appointment_table.put_item(Item=appointment_item)

        if ENABLE_EMAIL:
            send_email(doctor_email, "New Appointment", f"New appointment from {patient['name']} on {appointment_date} at {appointment_time}")
            send_email(patient_email, "Appointment Booked", f"Your appointment with Dr. {doctor.get('name')} is booked on {appointment_date} at {appointment_time}")

        publish_to_sns(f"New appointment booked by {patient['name']} with Dr. {doctor.get('name')} for {appointment_date} at {appointment_time}")

        flash('Appointment booked successfully', 'success')
        return redirect(url_for('patient_dashboard'))

    try:
        response = user_table.scan(FilterExpression=boto3.dynamodb.conditions.Attr('role').eq('doctor'))
        doctors = response['Items']
    except Exception as e:
        logger.error(f"Failed fetching doctors: {e}")
        doctors = []

    return render_template('book_appointment.html', doctors=doctors)

@app.route('/view_appointment/<appointment_id>')
def view_appointment_patient(appointment_id):
    if not is_logged_in() or session.get('role') != 'patient':
        flash('Unauthorized access', 'danger')
        return redirect(url_for('login'))

    try:
        response = appointment_table.get_item(Key={'appointment_id': appointment_id})
        appointment = response.get('Item')
        if not appointment:
            flash('Appointment not found', 'danger')
            return redirect(url_for('patient_dashboard'))

        if session.get('email') != appointment['patient_email']:
            flash('Access denied', 'danger')
            return redirect(url_for('patient_dashboard'))

        doctor = user_table.get_item(Key={'email': appointment['doctor_email']}).get('Item')
        return render_template("view_appointment_patient.html", appointment=appointment, doctor=doctor)

    except Exception as e:
        logger.error(f"Error fetching appointment details: {e}")
        flash('An error occurred while retrieving the appointment.', 'danger')
        return redirect(url_for('patient_dashboard'))


@app.route('/doctor/view-appointment/<appointment_id>')
def view_appointment_doctor(appointment_id):
    try:
        response = appointment_table.get_item(Key={'appointment_id': appointment_id})
        appointment = response.get('Item')
        if not appointment:
            flash("Appointment not found.", "danger")
            return redirect(url_for('doctor_dashboard'))

        if session.get('role') != 'doctor' or session.get('email') != appointment['doctor_email']:
            flash("Unauthorized access.", "danger")
            return redirect(url_for('doctor_dashboard'))

        patient = user_table.get_item(Key={'email': appointment['patient_email']}).get('Item')
        return render_template("view_appointment_doctor.html", appointment=appointment, patient=patient)

    except Exception as e:
        logger.error(f"Error fetching appointment: {e}")
        flash("An error occurred.", "danger")
        return redirect(url_for('doctor_dashboard'))


@app.route('/doctor/submit-diagnosis/<appointment_id>', methods=['POST'])
def submit_diagnosis(appointment_id):
    if not session.get('email') or session.get('role') != 'doctor':
        flash("Unauthorized access.", "danger")
        return redirect(url_for('login'))

    diagnosis = request.form.get('diagnosis')
    treatment_plan = request.form.get('treatment_plan')
    prescription = request.form.get('prescription')

    try:
        response = appointment_table.get_item(Key={'appointment_id': appointment_id})
        appointment = response.get('Item')

        if not appointment:
            flash("Appointment not found.", "danger")
            return redirect(url_for('doctor_dashboard'))

        if session.get('email') != appointment['doctor_email']:
            flash("Unauthorized access.", "danger")
            return redirect(url_for('doctor_dashboard'))

        appointment_table.update_item(
            Key={'appointment_id': appointment_id},
            UpdateExpression="SET diagnosis = :d, treatment_plan = :t, prescription = :p, #s = :s, updated_at = :u",
            ExpressionAttributeValues={
                ':d': diagnosis,
                ':t': treatment_plan,
                ':p': prescription,
                ':s': 'completed',
                ':u': datetime.utcnow().isoformat()
            },
            ExpressionAttributeNames={'#s': 'status'}
        )

        # Optional: send email or SNS notification here
        if ENABLE_EMAIL:
            send_email(
                appointment['patient_email'],
                "Appointment Completed",
                f"Dear {appointment['patient_name']}, your appointment with Dr. {appointment['doctor_name']} has been completed.\nDiagnosis: {diagnosis}\nTreatment Plan: {treatment_plan}"
            )

        publish_to_sns(
            f"Appointment completed by Dr. {appointment['doctor_name']} for {appointment['patient_name']}.",
            subject="Appointment Completed"
        )

        flash("Diagnosis submitted successfully.", "success")
    except Exception as e:
        logger.error(f"Error submitting diagnosis: {e}")
        flash("An error occurred while submitting diagnosis.", "danger")

    return redirect(url_for('doctor_dashboard'))

@app.route('/patient/profile')
def patient_profile():
    email = session.get('email')
    if not email or session.get('role') != 'patient':
        flash("Unauthorized access.", "danger")
        return redirect(url_for('login'))

    try:
        user = user_table.get_item(Key={'email': email}).get('Item')
        if not user:
            flash("User not found.", "danger")
            return redirect(url_for('login'))
        return render_template("patient_profile.html", user=user)
    except Exception as e:
        logger.error(f"Error fetching patient profile: {e}")
        flash("An error occurred.", "danger")
        return redirect(url_for('login'))
    
@app.route('/doctor/profile')
def doctor_profile():
    email = session.get('email')
    if not email or session.get('role') != 'doctor':
        flash("Unauthorized access.", "danger")
        return redirect(url_for('login'))

    try:
        user = user_table.get_item(Key={'email': email}).get('Item')
        if not user:
            flash("User not found.", "danger")
            return redirect(url_for('login'))
        return render_template("doctor_profile.html", user=user)
    except Exception as e:
        logger.error(f"Error fetching doctor profile: {e}")
        flash("An error occurred.", "danger")
        return redirect(url_for('login'))

    
@app.route('/forgot_password', methods=['GET', 'POST'])
def forgot_password():
    if request.method == 'POST':
        email = request.form.get('email')
        response = user_table.get_item(Key={'email': email})
        user = response.get('Item')
        if user:
            flash("Password reset link sent (simulated)", "success")
        else:
            flash("Email not found", "danger")
        return redirect(url_for('login'))
    return render_template('forgot_password.html')


# Error Pages
@app.errorhandler(404)
def not_found(e):
    return render_template("404.html"), 404

@app.errorhandler(500)
def server_error(e):
    return render_template("500.html"), 500

if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    debug = os.getenv('FLASK_ENV') == 'development'
    app.run(host='0.0.0.0', port=port, debug=debug)
